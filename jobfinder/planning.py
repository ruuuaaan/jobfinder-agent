"""Pure query planning. Search execution belongs to an explicitly configured provider."""
import hashlib
import json
import uuid
from datetime import datetime, timedelta
from .geography import normalize_city
from .models import RULE_VERSION, now
from .input_rules import recruitment_terms
from .company_selection import select_companies
from .role_expansion import RoleExpansion
from .entries import entry_task


def fingerprint(config):
    from .spec import matching_config
    config=matching_config(config)
    data=config.model_dump(mode='json')
    if config.rules_version=='CN_MAINLAND-3.9':
        data.pop('rules_version',None);data.pop('candidate_education',None)
        if not data['budget']['per_city']:data['budget'].pop('per_city')
    value = {"config": data, "rules": config.rules_version}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def cities(config):
    return list(dict.fromkeys(normalize_city(c) for c in config.target_cities))


def discovery_terms(config):
    return config.role_keywords if config.role_mode == "keywords" else config.company_scope_keywords


def build_plan(config, catalog=None, *, role_groups=None, company_assessments=None, knowledge=None, requested_companies=(), entry_knowledge=None):
    from .spec import spec_for_config
    spec=spec_for_config(config)
    config=spec.to_config()
    tasks, entry_tasks = [], []
    if role_groups is not None:
        role_groups = RoleExpansion.model_validate(role_groups).model_dump()
    selected_by_city, selection = select_companies(config,catalog,role_groups,company_assessments,knowledge,requested_companies)
    for city in cities(config):
        selected = selected_by_city[city]
        local = []
        for role in discovery_terms(config):
            for hiring_type in recruitment_terms(config.recruitment_type) or [""]:
                local.append(("direct", role, f'{city} {role} {hiring_type} 招聘 官网 工作地点'.replace("  ", " "), None))
        scope = " ".join(config.company_scope_keywords)
        local.append(("directory", "", f'{city} {scope} 机构 企业 名单 官方' if scope else f'{city} 国资委 监管企业 名录 官方', None))
        # Never filter employers by headquarters/registration; outside employers can hire here.
        for company in selected:
            name = company["legal_name"]
            reuse = entry_task(company,entry_knowledge if entry_knowledge is not None else knowledge,config,city,f'{city}-entry-{len(entry_tasks)+1}')
            if reuse:
                entry_tasks.append(reuse)
                continue
            term = " ".join(config.role_keywords) if config.role_mode == "keywords" else ""
            local.append(("enterprise", term, f'{name} {city} {term} 招聘'.replace("  ", " "), name))
        for index, (route, role, query, company) in enumerate(local):
            employer = next((c for c in selected if c['legal_name']==company), {})
            tasks.append(dict(task_id=f'{city}-{index+1}', target_city=city, role_keyword=role,
                              query=query, route=route, company=company, company_source=employer.get('source_url'),
                              company_selection=employer.get('selection_decision'),
                              reason='no_reusable_entry' if company else 'configured_discovery_query',
                              status="planned" if index < config.budget.city_searches(city) and not employer.get('company_budget_exhausted') else "budget_exhausted",
                              executed_at=None, search_tool=None, candidate_count=0))
    return dict(plan_id=str(uuid.uuid4()), created_at=now(), config_fingerprint=fingerprint(config),
                effective_config=config.model_dump(mode="json"), effective_search_spec=spec.model_dump(mode='json'), rule_version=config.rules_version,
                synonym_expansions=[], tasks=tasks, entry_tasks=entry_tasks, catalog_scope=(catalog or {}).get("scope"),
                role_groups=role_groups, company_assessments=company_assessments,
                requested_companies=list(requested_companies), company_selection=selection,
                coverage="逐城市小预算候选发现，不代表城市或全国完整覆盖")


def validate_receipt(config, plan, data):
    if plan["config_fingerprint"] != fingerprint(config) or data.get("config_fingerprint") != fingerprint(config):
        raise ValueError("查询计划/候选与本次完整条件或规则版本不同，请重新生成并执行查询")
    if data.get("plan_id") != plan["plan_id"] or data.get("mode") not in ("agent_assisted", "codex_assisted"):
        raise ValueError("新检索必须提供本次计划关联的真实搜索记录；旧候选只能 refresh/process-candidates")
    planned = {t["task_id"]: t for t in plan["tasks"]}
    logs = data.get("search_log", [])
    if len({x["task_id"] for x in logs}) != len(logs):
        raise ValueError("查询任务记录重复")
    for item in logs:
        task = planned.get(item["task_id"])
        if not task or any(item.get(k) != task[k] for k in ["query", "target_city", "role_keyword"]):
            raise ValueError("实际查询与计划不一致")
        if item.get("status") not in ("success", "failed", "restricted", "not_run", "budget_exhausted"):
            raise ValueError("缺少明确查询执行状态")
        if item["status"] in ("success", "failed", "restricted"):
            if task["status"] == "budget_exhausted" or not item.get("search_tool") or not item.get("executed_at"):
                raise ValueError("超预算查询或缺少执行时间/工具")
            if datetime.fromisoformat(item["executed_at"]) < datetime.fromisoformat(plan["created_at"]):
                raise ValueError("历史查询不能冒充本次新检索")
            if datetime.fromisoformat(item['executed_at']) > datetime.fromisoformat(now())+timedelta(minutes=5):
                raise ValueError('查询执行时间不能在未来')
    logged = {x["task_id"]: x for x in logs}
    for city in cities(config):
        used = plan.get('budget_used',{}).get('searches',{}).get(city,0)
        if used + sum(x.get('target_city') == city and x['status'] in ('success','failed','restricted') for x in logs) > config.budget.city_searches(city):
            raise ValueError('后续搜索超过本轮剩余查询预算')
    for candidate in data.get("candidates", []):
        ids = candidate.get("task_ids", [])
        if not ids or any(i not in logged or logged[i]["status"] != "success" for i in ids):
            raise ValueError("候选缺少本次成功查询关联")
        expected = {planned[i]["target_city"] for i in ids}
        if set(candidate.get("target_cities", [])) != expected:
            raise ValueError("候选城市与查询归属不一致")
    for item in logs:
        count = sum(item["task_id"] in c.get("task_ids", []) for c in data.get("candidates", []))
        if item.get("candidate_count") != count:
            raise ValueError("查询候选数量与记录不一致")
    return [(t | logged.get(t['task_id'], {'status': 'not_run' if t['status'] == 'planned' else t['status']}) | {'company': t.get('company'), 'company_source': t.get('company_source'), 'company_selection':t.get('company_selection'),
             'outcome':'not_found_this_run' if logged.get(t['task_id'],{}).get('status')=='success' and not logged[t['task_id']].get('candidate_count') else 'see_execution_status'}) for t in plan['tasks']]


def effective_config(config, source, overrides=None):
    from .spec import spec_for_config
    overrides = overrides or {}
    return {"values": config.model_dump(mode="json"), "effective_search_spec":spec_for_config(config).model_dump(mode='json'), "config_fingerprint": fingerprint(config),
            "origins": {k: ("本次CLI输入" if k in overrides else f"配置文件:{source}" if k in config.model_fields_set else "安全默认值") for k in config.model_fields},
            "note": "example/offline不是用户偏好；salary.currency业务默认不用于补全来源币种"}


def ensure_new_execution(plan, logs, output_root):
    executed=[datetime.fromisoformat(x['executed_at']) for x in logs if x.get('executed_at') and x.get('status') in ('success','failed','restricted')]
    if executed and datetime.fromisoformat(now())-max(executed)>timedelta(minutes=30):
        raise ValueError('查询记录超过30分钟交接窗口；请重新执行新计划或明确使用refresh')
    for path in output_root.glob('*/results.json'):
        previous=json.loads(path.read_text(encoding='utf-8'))
        discovery=previous.get('discovery',{})
        if discovery.get('plan_id')==plan['plan_id'] and discovery.get('operation')=='new_search':
            raise ValueError('该计划已执行过读取，不能再次冒充新搜索；请选择refresh或生成新计划')


def authorize_config(config, allow_example=False, offline=False):
    if config.config_kind=='offline' and not offline:
        raise ValueError('offline配置不能用于真实联网入口')
    if config.example_only:
        if not allow_example:
            raise ValueError("example_only 必须显式 --allow-example；演示不是用户条件")
    elif not offline and (config.config_kind != "user" or not config.user_input_confirmed):
        raise ValueError("请填写用户配置并明确 user_input_confirmed=true；不能靠删除 example_only 获得真实条件")


def extend_plan(config, plan, catalog, *, role_groups=None, company_assessments=None, knowledge=None, requested_companies=(), entry_knowledge=None):
    if plan['config_fingerprint'] != fingerprint(config):
        raise ValueError('不能扩展其他条件或规则版本的计划，请重新生成查询计划')
    if role_groups is not None and RoleExpansion.model_validate(role_groups).model_dump() != plan.get('role_groups'):
        raise ValueError('扩展不可改变岗位词组/性质，请生成新计划')
    role_groups = plan.get('role_groups')
    company_assessments = company_assessments if company_assessments is not None else plan.get('company_assessments')
    requested_companies = list(dict.fromkeys([*plan.get('requested_companies', []), *requested_companies]))
    previous_companies = [dict(company=name,target_city=city) for city,names in plan.get('budget_used',{}).get('companies',{}).items() for name in names]
    selected, selection = select_companies(config,catalog,role_groups,company_assessments,knowledge,requested_companies,[*plan['tasks'],*plan.get('entry_tasks',[]),*previous_companies])
    for city in cities(config):
        for company in selected[city]:
            local=[t for t in plan['tasks'] if t['target_city']==city]
            reuse = entry_task(company,entry_knowledge if entry_knowledge is not None else knowledge,config,city,f'{city}-entry-{len(plan.get("entry_tasks",[]))+1}')
            if reuse:
                plan.setdefault('entry_tasks',[]).append(reuse)
                continue
            term=" ".join(config.role_keywords) if config.role_mode=='keywords' else ''
            query=f"{company['legal_name']} {city} {term} 招聘".replace('  ',' ')
            if query in plan.get('query_history',[]) or any(t['query']==query for t in local):
                continue
            remaining = config.budget.city_searches(city)-plan.get('budget_used',{}).get('searches',{}).get(city,0)
            plan['tasks'].append(dict(task_id=f'{city}-{len(local)+1}',target_city=city,role_keyword=term,query=query,route='announcement_reverse' if company.get('discovery_route')=='announcement_reverse' else 'enterprise',company=company['legal_name'],company_source=company.get('source_url'),company_selection=company['selection_decision'],reason='no_reusable_entry',created_at=now(),status='planned' if len(local)<remaining and not company.get('company_budget_exhausted') else 'budget_exhausted',executed_at=None,search_tool=None,candidate_count=0))
    plan.setdefault('company_selection_history', []).append(selection)
    plan['requested_companies'] = requested_companies
    plan['company_assessments'] = company_assessments
    plan['extended_at']=now()
    return plan


def followup_plan(config, requests, logs, parent=None, budget_used=None):
    """Executable successor, not search results. Carry the original run's spent budget."""
    parent = parent or {}
    used = budget_used or {'searches':{}, 'requests':{}, 'companies':{}}
    history = list(parent.get('query_history',[]))
    history.extend(x['query'] for x in logs if x.get('status') in ('success','failed','restricted'))
    tasks, skipped, seen = [], [], set(history)
    for request in requests:
        city, company = request['target_city'], request['company']
        role = ' '.join(config.role_keywords) if config.role_mode == 'keywords' else ''
        query = f'{company} {city} {role} 招聘'.replace('  ',' ')
        if query in seen:
            skipped.append(dict(query=query,target_city=city,company=company,status='not_run',reason='already_planned_or_searched_this_run'))
            continue
        seen.add(query)
        remaining = config.budget.city_searches(city) - used.get('searches',{}).get(city,0)
        allocated = sum(t['target_city']==city and t['status']=='planned' for t in tasks)
        allowance = config.budget.city_requests(cities(config).index(city),len(cities(config)),used.get('requests',{}).get(city,0))
        readable = allowance is None or allowance > 0
        tasks.append(dict(task_id=f'{city}-fallback-{len(tasks)+1}',target_city=city,company=company,
            role_keyword=role,query=query,route='enterprise_fallback',company_source=request.get('source_url'),
            status='planned' if allocated < remaining and readable else 'budget_exhausted',
            reason=request['reason'] if readable else 'request_budget_exhausted',executed_at=None,search_tool=None,candidate_count=0))
    return dict(plan_id=str(uuid.uuid4()),parent_plan_id=parent.get('plan_id'),created_at=now(),
        rule_version=config.rules_version,config_fingerprint=fingerprint(config),effective_config=config.model_dump(mode='json'),
        tasks=tasks,entry_tasks=[],skipped=skipped,budget_used=used,query_history=list(dict.fromkeys(history)),
        status='awaiting_assistant_search' if any(t['status']=='planned' for t in tasks) else 'no_executable_search',
        coverage='仅后续合并查询计划；尚未执行在线搜索，不表示企业无岗位')
