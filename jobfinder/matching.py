from __future__ import annotations
import hashlib
import json
import re
import unicodedata
from urllib.parse import urlsplit
from datetime import datetime
from .models import Decision, ExperienceConfig, Job, MatchResult, SearchConfig, SHANGHAI
from .geography import evaluate_location, split_locations
from .input_rules import recruitment_terms

EDUCATION = {"小学": 0, "初中": 0.5, "高中": 1, "中专": 1, "中技": 1, "职高": 1, "大专": 2, "专科": 2, "本科": 3, "硕士": 4, "博士": 5}


def decision(state, reason):
    return Decision(state=state, reason=reason)


def education_match(raw, config):
    if not config.enabled:
        return decision("PASS", "用户明确不设学历筛选，保留岗位原文；未披露不改写为企业学历不限")
    if not raw:
        return decision("UNKNOWN", "学历未披露，不等于学历不限")
    if re.search(r"学历不限|不限学历|no (?:degree|education) requirement", raw, re.I):
        return decision("PASS" if config.mode == "candidate_qualification" or config.include_unrestricted else "FAIL", "岗位明确学历不限")
    # Preferences must not raise the minimum; conditional exceptions need human review.
    if re.search(r"放宽|优秀者|或.*经验|经验.*或", raw):
        return decision("UNKNOWN", "学历含条件性放宽，需人工确认最低门槛："+raw)
    mandatory = re.sub(r"[，,；;。]?\s*(?:硕士|博士|本科|大专)(?:学历)?优先[^，,；;。]*", "", raw)
    levels = [v for k, v in EDUCATION.items() if k in mandatory]
    if not levels:
        return decision("UNKNOWN", "学历原文无法规范化")
    if len(set(levels)) > 1 and re.search(r'必须|同时|取得|并', mandatory):
        return decision("UNKNOWN", "存在多重学历条件，不能仅采用其中较低门槛："+raw)
    required = min(levels)
    user = EDUCATION[config.value]
    if config.mode == "candidate_qualification" or config.comparison == "at_most":
        ok = user >= required
    elif config.comparison == "at_least":
        ok = required >= user
    else:
        ok = user == required
    return decision("PASS" if ok else "FAIL", f"{config.mode}/{config.comparison}：岗位门槛原文 {raw}")


def parse_salary(job: Job) -> dict:
    raw = job.salary_raw or ""
    result = dict(raw=job.salary_raw, low=None, high=None, currency=job.salary_currency,
                  period=job.salary_period, basis=job.salary_basis, pay_count=None,
                  monthly=None, annual=None, composition_unknown=False)
    if not result["currency"]:
        if re.search(r"人民币|(?<![A-Za-z])CNY(?![A-Za-z])|(?<![A-Za-z])RMB(?![A-Za-z])", raw, re.I):
            result["currency"] = "CNY"
        elif re.search(r"美元|(?<![A-Za-z])USD(?![A-Za-z])", raw, re.I):
            result["currency"] = "USD"
    if not result["period"]:
        for pattern, period in [(r"月薪|/月|每月|月度", "month"), (r"年薪|/年|每年", "year"), (r"/天|日薪", "day"), (r"/小时|时薪", "hour")]:
            if re.search(pattern, raw):
                result["period"] = period
                break
    if not result["basis"]:
        result["basis"] = "gross" if "税前" in raw else "net" if "税后" in raw else None
    pay = re.search(r"(\d{2})薪", raw)
    if pay:
        result["pay_count"] = int(pay[1])
    result["composition_unknown"] = not (job.salary_component == "fixed" or re.search(r"固定(?:月薪|年薪|薪资)|基本工资|基础工资", raw)) or bool(re.search(r"综合|含.{0,8}(?:奖金|提成|绩效)|底薪\s*[+＋]|浮动|总包", raw))
    if re.search(r"面议|negotiable", raw, re.I):
        return result
    m = re.search(r"(\d+(?:\.\d+)?)\s*([kK万千]?)\s*[-—–~～至]\s*(\d+(?:\.\d+)?)\s*([kK万千]?)", raw)
    units = {"": 1, "k": 1000, "千": 1000, "万": 10000}
    if m:
        result["low"] = float(m[1]) * units[(m[2] or m[4]).lower()]
        result["high"] = float(m[3]) * units[m[4].lower()]
    else:
        m = re.search(r"(?:月薪|年薪|税前|税后|CNY|RMB|人民币)\s*(\d+(?:\.\d+)?)\s*([kK万千]?)", raw, re.I)
        if m:
            result["low"] = result["high"] = float(m[1]) * units[m[2].lower()]
    if result["low"] is not None and result["high"] < result["low"]:
        result["low"] = result["high"] = None
    if result["period"] in ("month", "year"):
        result["monthly" if result["period"] == "month" else "annual"] = [result["low"], result["high"]]
    return result


def salary_match(job, config):
    if not config.enabled:
        return decision("PASS", "未启用薪酬筛选，未对金额或披露完整性设门槛；保留薪资原文，不补数值")
    s = parse_salary(job)
    if any(s[k] is None for k in ["low", "high", "currency", "period", "basis"]) or s["composition_unknown"]:
        return decision("UNKNOWN", "薪资金额/币种/周期/税前税后/固定浮动构成未完整明确")
    if (s["currency"], s["period"], s["basis"]) != (config.currency, config.period, config.basis):
        return decision("UNKNOWN", "薪资口径不同，本阶段不换汇、不月年混算")
    v = s["low"] if config.match_mode == "conservative" else s["high"]
    reason = "区间下限达到期望" if config.match_mode == "conservative" else "区间上限达到期望，需谈薪（不保证取得上限）"
    return decision("PASS" if v >= config.amount else "FAIL", reason if v >= config.amount else "公布薪资不满足选择的匹配方式")


def experience_match(raw, config):
    if not raw or not raw.strip():
        return decision("UNKNOWN", "岗位经验未披露，不等于经验不限")
    if re.search(r"放宽|优秀者|优先|或", raw):
        return decision("UNKNOWN", "经验含优先或条件性要求，最低年限待核实：" + raw)
    unrestricted = r"(?:不限|(?:工作)?经验不限|不限(?:工作)?经验)[。.!！]?"
    no_experience = r"(?:无(?:工作)?经验(?:要求)?|无需(?:工作)?经验)[。.!！]?"
    if re.fullmatch(unrestricted, raw.strip()):
        allowed = config.mode == "candidate_qualification" or config.include_unrestricted
        return decision("PASS" if allowed else "FAIL", "岗位明确经验不限；与未披露分开")
    if re.fullmatch(no_experience, raw.strip()):
        required = 0.0
    else:
        if re.search(r"经验不限|不限(?:工作)?经验|无(?:工作)?经验|无需(?:工作)?经验", raw):
            return decision("UNKNOWN", "不限或无经验文字带有其他条件，不能推定最低年限：" + raw)
        # Compare the stated minimum, never the upper end of 3–5 years.
        ranges = list(re.finditer(r"(\d+(?:\.\d+)?)\s*[-—–~～至]\s*(\d+(?:\.\d+)?)\s*年", raw))
        remaining = re.sub(r"\d+(?:\.\d+)?\s*[-—–~～至]\s*\d+(?:\.\d+)?\s*年", "", raw)
        singles = re.findall(r"(\d+(?:\.\d+)?)\s*年", remaining)
        if re.search(r"年(?:以下|以内)|不超过|至多|最多|不足|超过|多于", raw):
            return decision("UNKNOWN", "原文不是明确最低年限，不能按上限或严格不等式猜测：" + raw)
        if len(ranges) + len(singles) != 1:
            return decision("UNKNOWN", "经验年限缺失、多重或无法可靠规范化：" + raw)
        if ranges:
            required = float(ranges[0][1])
            if required > float(ranges[0][2]):
                return decision("UNKNOWN", "岗位经验区间倒置")
        else:
            required = float(singles[0])
    if config.mode == "candidate_qualification" or config.comparison == "at_most":
        ok = required <= config.years
    elif config.comparison == "at_least":
        ok = required >= config.years
    else:
        ok = required == config.years
    return decision("PASS" if ok else "FAIL", f"{config.mode}/{config.comparison}：岗位最低经验{required:g}年，输入{config.years:g}年；未核验全部资历")


def recruitment_match(raw, selected):
    observed = recruitment_terms(raw)
    if not observed:
        return decision("UNKNOWN", "招聘类型未披露，不从职责或学历推断")
    ok = bool(set(observed).intersection(recruitment_terms(selected)))
    return decision("PASS" if ok else "FAIL", "招聘类型按明确标签及同义词任一匹配；不证明毕业年份等完整资格")


def job_key(job):
    source_identity=[source_namespace(job),job.source_id] if job.source_id else job.source_url
    return hashlib.sha256(json.dumps([job.source_channel, source_identity, job.company, job.title, job.location_raw, job.locations, job.level], ensure_ascii=False).encode()).hexdigest()[:20]


def source_namespace(job):
    return (urlsplit(job.source_url).hostname or '').casefold().removeprefix('www.')


def same_source_id(a, b):
    return bool(a.source_id and a.source_id==b.source_id and a.source_channel==b.source_channel
                and source_namespace(a)==source_namespace(b))


def identity_text(value):
    """Compare observed names ignoring layout only; keep original display text."""
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', value or '')).casefold()


def deduplicate(jobs: list[Job]) -> list[Job]:
    output = []
    for job in jobs:
        duplicate = None
        for old in output:
            dimensions = lambda j: (identity_text(j.company), identity_text(j.title), identity_text(j.location_raw), tuple(identity_text(p) for p in j.locations), j.location_mode, identity_text(j.level))
            same_dimensions = dimensions(job) == dimensions(old)
            same_id = same_source_id(job,old)
            same_content = job.description.strip() == old.description.strip() and job.requirements == old.requirements
            distinct_ids = bool(job.source_id and old.source_id and not same_id)
            distinct_details = bool(job.detail_url and old.detail_url and job.detail_url != old.detail_url and not same_id)
            if same_dimensions and not distinct_ids and not distinct_details and (same_id or same_content):
                duplicate = old
                break
        if duplicate is None:
            output.append(job.model_copy(deep=True))
        else:
            duplicate.evidence.extend(e for e in job.evidence if e not in duplicate.evidence)
            duplicate.discovery_task_ids=list(dict.fromkeys(duplicate.discovery_task_ids+job.discovery_task_ids))
            duplicate.discovery_cities=list(dict.fromkeys(duplicate.discovery_cities+job.discovery_cities))
            duplicate.validity_checked_at=max(filter(None,[duplicate.validity_checked_at,job.validity_checked_at]),default=None)
            for key,values in job.conflicts.items():
                duplicate.conflicts[key]=list(dict.fromkeys(duplicate.conflicts.get(key,[])+values))
            for key in ["salary_raw", "education_raw", "experience_raw", "posted_at", "deadline", "open_status", "application_url", "requirements", "description"]:
                a, b = getattr(duplicate, key), getattr(job, key)
                if a != b:
                    duplicate.conflicts[key] = list(dict.fromkeys([str(a), str(b)] + duplicate.conflicts.get(key, [])))
            if {'OPEN','CLOSED'}.issubset(duplicate.conflicts.get('open_status',[])):
                duplicate.open_status='UNKNOWN'
    return output


def evaluate(job: Job, config: SearchConfig) -> list[MatchResult]:
    from .spec import matching_config
    from .education import RULES_VERSION, compare_candidate
    config=matching_config(config)
    from .geography import belongs, normalize_city, same_city_evidence
    results = []
    current_date = datetime.now(SHANGHAI).date()
    for raw in split_locations(job):
        geo = evaluate_location(job, raw, config.target_cities)
        active_conflicts = dict(job.conflicts)
        places = list(dict.fromkeys(([raw] if raw else []) + job.conflicts.get('location_raw', [])))
        if job.conflicts.get('location_raw') and same_city_evidence(places):
            active_conflicts.pop('location_raw', None)
        elif job.conflicts.get('location_raw'):
            alternatives = [evaluate_location(job, value, config.target_cities) for value in places]
            if alternatives and all(x.state == 'FAIL' for x in alternatives):
                geo=geo.model_copy(update={'state':'FAIL','reason':'所有地点证据均不满足目标地域；原文差异不改变排除结论'})
            else:
                geo=geo.model_copy(update={'state':'UNKNOWN','reason':'岗位地点原文冲突：'+' / '.join(job.conflicts['location_raw'])})
        d = {"geography": decision(geo.state, geo.reason)}
        d["role"] = decision("PASS", "用户明确不限具体职位；另行核验企业范围") if config.role_mode == "any" else decision("PASS" if any(k.casefold() in job.title.casefold() for k in config.role_keywords) else "FAIL", "仅比较用户明确的岗位关键词，不扩展为销售/项目经理")
        if config.role_mode == 'keywords' and job.role_review is not None:
            review = job.role_review
            same_context = review.get('role_keywords') == config.role_keywords
            verdict = review.get('decision') if same_context else 'unknown'
            if verdict in ('related','out_of_scope') and review.get('quote') and review.get('content_hash'):
                d['role'] = decision('PASS' if verdict == 'related' else 'FAIL','有原文证据的助手职能判断：'+review.get('rationale',''))
            elif d['role'].state != 'PASS':
                d['role'] = decision('UNKNOWN','标题未命中不证明职能不符；须按职责核对岗位范围')
        if config.company_scope_keywords:
            proved = [k for k in config.company_scope_keywords if any(e.method == "company_profile_original" and e.field_quotes.get("company") == job.company and e.field_quotes.get("company_scope:"+k.casefold()) for e in job.evidence)]
            d["company_scope"] = decision("PASS" if proved else "UNKNOWN", "企业范围原始证据："+"、".join(proved) if proved else "企业是否属于所选范围尚无原始证据，不能仅凭岗位标题或MCN经验要求认定")
        d["education"] = (decision(*compare_candidate(job.education_raw,config.candidate_education))
            if config.rules_version==RULES_VERSION else education_match(job.education_raw, config.education))
        d["salary"] = salary_match(job, config.salary)
        if not job.details_complete:
            d['details'] = decision('UNKNOWN', '仅取得搜索摘要或列表，职责和要求待确认' if job.evidence_level in ('search_snippet','onsite_list') else '原站具体岗位记录已取得，但职责/任职要求详情未完整读取，不能标记为符合')
        d["vacancy"] = decision("PASS" if job.open_status == "OPEN" else "FAIL" if job.open_status == "CLOSED" else "UNKNOWN", f"岗位开放状态 {job.open_status}，页面可达不代表仍开放")
        if job.deadline:
            try:
                if datetime.fromisoformat(job.deadline).date() < current_date:
                    d["vacancy"] = decision("FAIL", "已超过原文截止日期")
            except ValueError:
                d["vacancy"] = decision("UNKNOWN", "截止日期格式待核实")
        experience = config.experience
        if experience is None and config.candidate_experience_years is not None:
            experience = ExperienceConfig(mode="candidate_qualification", years=config.candidate_experience_years)
        if experience is not None:
            d["experience"] = experience_match(job.experience_raw, experience)
        if config.recruitment_type:
            d["recruitment_type"] = recruitment_match(job.recruitment_type, config.recruitment_type)
        if config.posted_after or config.posted_before:
            try:
                posted = datetime.fromisoformat(job.posted_at).date()
                ok = (not config.posted_after or posted >= config.posted_after) and (not config.posted_before or posted <= config.posted_before)
                d["date"] = decision("PASS" if ok else "FAIL", "原始发布日期范围比较")
            except (TypeError, ValueError):
                d["date"] = decision("UNKNOWN", "发布日期未明确，不用抓取时间替代")
        d["evidence"] = decision("PASS" if job.evidence and job.evidence_level not in ('search_snippet','onsite_list') and any(e.method != "search_snippet" for e in job.evidence) else "UNKNOWN", "原始字段证据是否存在；摘要及列表须核验详情")
        pending = [v.reason for v in d.values() if v.state == "UNKNOWN"] + job.notes
        if not job.application_url:
            pending.append("具体投递链接未披露；请核实原文投递方式")
        if job.requirements:
            pending.append("专业、毕业年份及其他任职要求保留原文，未全面核验")
        if active_conflicts:
            d["conflicts"] = decision("UNKNOWN", "多来源字段冲突，需人工核实")
            pending.append("多来源字段冲突：" + ",".join(active_conflicts))
        states = [v.state for v in d.values()]
        status = "EXCLUDED" if "FAIL" in states else "REVIEW" if "UNKNOWN" in states else "MATCH"
        if status == "REVIEW" and config.unknown_policy == "exclude":
            status = "EXCLUDED"
        if config.salary.enabled and config.salary.match_mode == "possible" and d["salary"].state == "PASS":
            pending.append("需谈薪，公开区间上限不是承诺薪资")
        matched = [normalize_city(t) for t in config.target_cities if geo.state=="PASS" and belongs(geo.city,normalize_city(t))]
        results.append(MatchResult(job=job, original_job_key=job_key(job), geo=geo, dimensions=d, status=status, pending=list(dict.fromkeys(pending)),matched_cities=list(dict.fromkeys(matched)),rule_version=config.rules_version))
    return results
