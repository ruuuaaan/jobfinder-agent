from __future__ import annotations
import json
import re
from pathlib import Path
from datetime import datetime
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from .models import RULE_VERSION, now, SHANGHAI
from .matching import parse_salary

HEADERS = ["公司名称", "岗位名称", "岗位描述", "薪资水平", "投递链接", "工作地点原文", "规范化城市", "地域判断", "学历要求", "经验要求", "企业属性", "发布日期", "截止日期", "岗位状态", "来源渠道", "来源链接", "核验时间", "匹配结论", "待确认事项", "任职要求", "投递方式", "匹配条件", "规则版本", "原始岗位关联", "匹配计算时间"]
SUMMARY_HEADERS = ['公司名称','岗位名称','岗位描述','薪资水平','投递链接','工作地点','学历要求','经验要求','岗位来源','待确认说明']


def summary_review_note(status, pending, historical=False):
    notes=['历史记录（本次未重新核验）'] if historical else []
    if status=='REVIEW':
        notes.append('待确认：'+'；'.join(pending or ['信息尚未完整核验']))
    return '\n'.join(notes)

SOURCE_LABELS = {
    'official_career': '企业官网', 'zhaopin': '智联招聘', 'boss': 'BOSS直聘',
    'liepin': '猎聘', '51job': '前程无忧', 'bendibao_web': '本地宝网站',
    'bendibao_wechat': '本地宝公众号', 'manual_import': '人工导入',
}


def source_label(channel):
    return SOURCE_LABELS.get(channel, channel or '未披露')


def safe_text(value):
    if value is None:
        return None
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(value))
    return text[:32760]


def display_datetime(value):
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is not None:
            result = result.astimezone(SHANGHAI)
        return result.replace(tzinfo=None)
    except (TypeError, ValueError):
        return value


def excerpt(value):
    if isinstance(value, str) and len(value)>450:
        return value[:450]+"…（完整内容见JSON）"
    return value


def sheet(book, name, headers, rows):
    ws = book.create_sheet(name)
    ws.append(headers)
    for row in rows:
        ws.append([safe_text(v) if not isinstance(v, (int, float, bool, datetime)) else v for v in row])
    for row in ws:
        for cell in row:
            # Explicit string type blocks =,+,-,@ and whitespace-prefixed formula injection.
            if isinstance(cell.value, str):
                cell.data_type = "s"
            cell.font = Font(name="Microsoft YaHei", size=10, color="203044")
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            if isinstance(cell.value, datetime):
                cell.number_format = "yyyy-mm-dd hh:mm"
    for cell in ws[1]:
        cell.font = Font(name="Microsoft YaHei", size=10, bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="25466B")
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[1].height = 30
    for i in range(1, len(headers)+1):
        header = headers[i-1]
        width = 48 if header in ("岗位描述", "任职要求", "待确认事项", "待确认说明", "说明", "匹配条件") else 36 if "链接" in header or "URL" in header else 24
        ws.column_dimensions[get_column_letter(i)].width = width
        for row in range(2, ws.max_row+1):
            cell = ws.cell(row, i)
            if ("链接" in header or "URL" in header) and isinstance(cell.value, str) and re.fullmatch(r"https?://[^\s]+", cell.value):
                cell.hyperlink = cell.value
                cell.font = Font(name="Microsoft YaHei", size=10, color="1565C0", underline="single")
    for row in range(2, ws.max_row+1):
        max_lines = max((len(str(c.value or "")) // max(10, int(ws.column_dimensions[c.column_letter].width/2)) + str(c.value or "").count("\n") for c in ws[row]), default=1)
        ws.row_dimensions[row].height = min(400, max(32, (max_lines+1)*14))
    ws.freeze_panes = "C2" if name in ("匹配岗位", "待确认岗位") else "A2"
    ws.auto_filter.ref = ws.dimensions
    ws.sheet_view.showGridLines = False
    return ws


def export_bundle(output: Path, config, jobs, results, reports, companies, discovery, events=None, mode="real", annotate_reviews=False):
    from .spec import spec_for_config
    raw_input=getattr(config,'_raw_input',None)
    spec=spec_for_config(config)
    config=spec.to_config()
    from .statistics import city_statistics
    from .planning import fingerprint
    output.mkdir(parents=True, exist_ok=True)
    if (output / "results.xlsx").exists() or (output / "results.json").exists():
        raise FileExistsError("目标已有结果，请指定新的输出目录，不覆盖")
    if mode == "real" and any(j.synthetic for j in jobs):
        raise ValueError("合成岗位禁止进入真实结果目录")
    company_map = {c["legal_name"]: c for c in companies if c.get("legal_name")}
    book = Workbook()
    book.remove(book.active)
    for status, name in [("MATCH", "匹配岗位"), ("REVIEW", "待确认岗位")]:
        rows = []
        for r in results:
            if r.status != status or r.geo.state == "FAIL":
                continue
            j = r.job
            c = company_map.get(j.company, {})
            attrs = f"上市(来源时点)={c.get('listed_status') if c.get('listed_status') is not None else '未知'}；国有(来源时点)={c.get('soe_status') or '未知'}；名单日期={c.get('list_date') or '未知'}；当前属性未单独复核"
            if config.company_scope_keywords:
                scope=r.dimensions.get('company_scope')
                attrs += '；企业范围：'+scope.state+' '+scope.reason
            # Brief descriptive display; full original remains in structured evidence.
            desc = j.description if len(j.description) <= 450 else j.description[:450] + "…（完整原文见JSON）"
            req = j.requirements or ""
            if len(req)>450:
                req = req[:450]+"…（完整原文见JSON）"
            read_at = max((e.retrieved_at for e in j.evidence), default=None)
            rows.append([j.company,j.title,desc,j.salary_raw,j.application_url,r.geo.raw,r.geo.city,r.geo.state,j.education_raw,j.experience_raw,attrs,display_datetime(j.posted_at),display_datetime(j.deadline),j.open_status,j.source_channel,j.source_url,display_datetime(read_at),r.status,"；".join(r.pending),req,j.application_method,"；".join(f"{k}:{v.state} {v.reason}" for k,v in r.dimensions.items()),r.rule_version,r.original_job_key,display_datetime(r.checked_at)])
        sheet(book, name, HEADERS, rows)
    catalog_headers = ["公司名称", "上市状态", "国有属性", "集团关系", "注册地", "总部所在地", "上市地", "官网URL", "招聘入口URL", "名单来源链接", "版本日期", "发现路径", "入口核验状态", "说明"]
    sheet(book,"企业检查清单",catalog_headers,[[str(c.get(k) or '')[:150]+'（完整证据见JSON）' if k=="notes" and len(str(c.get(k) or ''))>150 else c.get(k) for k in ["legal_name","listed_status","soe_status","group_relation","registered_location","headquarters_location","listing_location","official_url","career_url","source_url","version_date","discovery_kind","verification_status","notes"]] for c in companies])
    report_headers=["渠道", "来源链接", "访问状态", "解析状态", "本次提取数", "说明", "历史访问状态", "历史解析状态", "历史提取数", "历史说明"]
    sheet(book,"渠道执行报告",report_headers,[[r.get(k) for k in ["channel","url","access_status","parse_status","jobs_extracted","reason","historical_access_status","historical_parse_status","historical_jobs_extracted","historical_reason"]] for r in reports])
    candidate_rows=[]
    for c in discovery.get('candidates',[]):
        r=next((r for r in reports if r.get('url')==c['url']),{})
        candidate_rows.append([c.get('company'),c.get('title'),c.get('candidate_type'),','.join(c.get('target_cities',[])),c.get('url'),r.get('access_status'),r.get('parse_status'),r.get('reason'),'候选线索，不是已核验岗位'])
    seen={c.get('url') for c in discovery.get('candidates',[])}
    for r in reports:
        for link in r.get('discovered_links',[]):
            if link['url'] in seen:
                continue
            seen.add(link['url'])
            processed=next((p for p in reports if p.get('url')==link['url']),{})
            candidate_rows.append([r.get('company'),link.get('title'),'原文导航发现',','.join(r.get('target_cities',[])),link['url'],processed.get('access_status',link.get('status')),processed.get('parse_status','not_run'),processed.get('reason','原站明示链接，仍须访问策略与具体岗位核验'),'来源页：'+str(link.get('parent_url') or r.get('url'))])
    if discovery.get('operation') or candidate_rows:
        sheet(book,'候选链接清单',['企业线索','页面标题','候选类型','查询归属城市','候选链接','访问状态','解析状态','待处理原因','说明'],candidate_rows)
    counts = {s:sum(r.status==s for r in results) for s in ["MATCH","REVIEW","EXCLUDED"]}
    conditions = [["数据模式",mode],["example_only",config.example_only],["业务范围","CN_MAINLAND；仅实际工作地点；港澳台及海外、远程不纳入"],["生成时间",now()],["规则版本",config.rules_version],["发现方式",discovery.get("provider")],["搜索状态",discovery.get("status")],["覆盖范围",discovery.get("coverage")],["统计",json.dumps(counts,ensure_ascii=False)],["说明","UNKNOWN不得标记符合；访问失败不等于无岗位；HTTP200不代表仍开放；核验时间是原始证据读取时间，匹配计算时间另列；未自动投递"],["时区","Asia/Shanghai"]]
    conditions.extend([["检索条件："+k,json.dumps(v,ensure_ascii=False)] for k,v in config.model_dump(mode="json").items()])
    conditions.extend([["实际查询", json.dumps(q,ensure_ascii=False)] for q in discovery.get("search_log", [])])
    conditions.extend([["历史查询（本次未执行）",json.dumps(q,ensure_ascii=False)] for q in discovery.get("historical_search_log",[])])
    sheet(book,"检索条件与说明",["项目","说明"],conditions)
    city_rows=city_statistics(config,jobs,results,reports,discovery)
    if discovery.get("operation"):
        keys=["city","queries_planned","queries_executed","companies_discovered","companies_checked","candidate_links","pages_read","pages_parsed","jobs_extracted","jobs_reused","MATCH","REVIEW","EXCLUDED","restricted","failed","budget_exhausted","unchecked","queries_unchecked","note"]
        sheet(book,"逐城市执行统计",["城市","计划查询","执行查询","发现企业","检查企业","候选链接","读取页面","解析页面","本次提取岗位","复用证据岗位","匹配","待确认","排除","受限","失败","预算耗尽","未检查候选","未执行查询","说明"],[[row[k] for k in keys] for row in city_rows])
    book.save(output/"results.xlsx")
    # Keep source provenance ninth and review notes last, separate from duties.
    summary=Workbook()
    summary.remove(summary.active)
    rows=[]
    emitted=set()
    for r in results:
        if r.status not in ('MATCH','REVIEW') or r.geo.state=='FAIL' or r.original_job_key in emitted:
            continue
        emitted.add(r.original_job_key)
        j=r.job
        pending=r.pending or [v.reason for v in r.dimensions.values() if v.state=='UNKNOWN']
        rows.append([j.company,j.title,j.description,j.salary_raw or '未披露',
                     j.application_url or j.detail_url or j.source_url,j.location_raw or '未披露',
                     j.education_raw or '未披露',j.experience_raw or '未披露',source_label(j.source_channel),summary_review_note(r.status,pending)])
    sheet(summary,'岗位汇总',SUMMARY_HEADERS,rows)
    summary.save(output/'岗位汇总.xlsx')
    payload={"schema_version":"2.1","rule_version":config.rules_version,"generated_at":now(),"mode":mode,"config":config.model_dump(mode="json"),"effective_search_spec":spec.model_dump(mode="json"),"raw_input":(discovery.get("effective_config") or {}).get("raw_input",raw_input),"jobs":[j.model_dump(mode="json") for j in jobs],"results":[r.model_dump(mode="json") | {"salary_normalized":parse_salary(r.job)} for r in results],"counts":counts,"reports":reports,"companies":companies,"discovery":discovery,"network_events":events or []}
    payload.update(schema_version="3.2",config_fingerprint=fingerprint(config),city_statistics=city_rows,global_unique_jobs=len(jobs),global_unique_counts={s:len({r.original_job_key for r in results if r.status==s}) for s in counts},exported_at=now(),current_network_request_count=0 if discovery.get('operation')=='reexport' else len(events or []),network_events_origin='historical' if discovery.get('operation')=='reexport' else 'current_run')
    for filename, rows in [("query-log.jsonl",discovery.get("search_log",[])),("candidates.jsonl",discovery.get("candidates",[])),("network.jsonl",events or [])]:
        (output/filename).write_text("".join(json.dumps(row,ensure_ascii=False)+"\n" for row in rows),encoding="utf-8")
    (output/"effective-config.json").write_text(json.dumps(discovery.get("effective_config") or {"values":config.model_dump(mode="json"),"config_fingerprint":fingerprint(config)},ensure_ascii=False,indent=2),encoding="utf-8")
    (output/"results.json").write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8")
    if discovery.get('follow_up_plan'):
        (output/'follow-up-plan.json').write_text(json.dumps(discovery['follow_up_plan'],ensure_ascii=False,indent=2),encoding='utf-8')
    if discovery.get('traversal_progress'):
        (output/'traversal-progress.json').write_text(json.dumps(discovery['traversal_progress'],ensure_ascii=False,indent=2),encoding='utf-8')
    with (output/"excluded.jsonl").open("w",encoding="utf-8") as f:
        for r in results:
            if r.status=="EXCLUDED":
                f.write(r.model_dump_json()+"\n")
    return payload
