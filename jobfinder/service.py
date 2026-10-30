import json
import hashlib
from pathlib import Path
from urllib.parse import urlsplit
from collections import deque
from .models import SearchConfig, Job, Candidate, now
from .search import CodexCandidateProvider, UnconfiguredSearchProvider
from .network import Fetcher, FetchError
from .robots_cache import RobotsCache
from .parsers import parse_html, ParseError, links, enrich
from .matching import deduplicate, evaluate
from .exporter import export_bundle
from .catalog import read_rows
from .planning import cities, validate_receipt, fingerprint, ensure_new_execution, followup_plan
from .entry_reader import SharedReader, read_entry_steps
from .navigation import verify_navigation, canonical, discover_career
from .page_reader import extract_page_steps
from .traversal import scope_id, initial_state
from .company_scope import scope_evidence
from .resources import ResourceStore, record_candidates, record_task_companies, name_key
from .announcements import verify_announcement_employer, record_profile, read_announcement
from .execution_audit import build_execution_audit, is_content_event

from .paths import WORKSPACE as ROOT


def local_path(path, base=ROOT):
    p = (base / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
    if not p.is_relative_to(ROOT):
        raise ValueError("本阶段只读取项目内显式提供的文件，请先将有权使用的材料复制到项目")
    return p


def output_path(path, mode):
    p = local_path(path)
    expected = ROOT / "outputs" / mode
    if not p.is_relative_to(expected) or p == expected:
        raise ValueError(f"输出必须位于 outputs/{mode}/<独立运行目录>，不得混用模拟与真实结果")
    if p.exists() and any(p.iterdir()):
        raise ValueError("输出目录不是空目录，请选择新运行目录")
    return p


def load_config(path):
    from .spec import matching_config
    return matching_config(json.loads(local_path(path).read_text(encoding="utf-8-sig")))


def import_jobs(candidate):
    if not candidate.authorized_import:
        raise ValueError("人工文件未声明有权提供")
    path = local_path(candidate.local_file)
    data = path.read_bytes()
    if len(data)>10000000:
        raise ValueError("导入文件大于10MB，需要另行审核")
    if path.suffix.lower() in (".html", ".htm"):
        return parse_html(data,candidate,str(path))
    if path.suffix.lower() in (".json", ".csv", ".xlsx"):
        records = json.loads(data.decode("utf-8-sig")) if path.suffix.lower()==".json" else read_rows(path)
        output=[]
        for record in records:
            record.setdefault("source_url",candidate.url)
            record["source_channel"]="manual_import"
            record["synthetic"]=candidate.synthetic
            record.pop("evidence",None)
            job=Job.model_validate(record)
            job.notes.append("用户有权提供的人工导入材料；不代表自动读取原站成功")
            output.append(enrich(job,data,"authorized_manual_import",str(path)))
        return output
    raise ParseError("附件类型尚未实现可靠岗位拆分；PDF文本/扫描件/图片须人工处理，不能记为解析成功")


def run(config, output, candidates_file=None, policies_file=None, mode="real", catalog_file=None, operation="provided_links", plan_file=None, effective=None, resource_path=None, resume_file=None, resume_budget='remaining'):
    output=output_path(output,mode)
    provider=CodexCandidateProvider(local_path(candidates_file)) if candidates_file else UnconfiguredSearchProvider()
    candidates, discovery=provider.discover(config)
    plan = {}
    resume_states, prior_jobs = {}, []
    if resume_file:
        checkpoint=json.loads(local_path(resume_file).read_text(encoding='utf-8-sig'))
        if checkpoint.get('schema_version')!='traversal-run-1' or checkpoint.get('config_fingerprint')!=fingerprint(config):
            raise ValueError('遍历进度与当前配置/规则不一致，不能混用')
        if resume_budget not in ('remaining','new_run'):
            raise ValueError('恢复预算只能为remaining或new_run')
        resume_states={key:value for key,value in checkpoint['states'].items() if not value['complete']}
        candidates=[Candidate.model_validate(state['candidate']) for state in resume_states.values()]
        from .traversal import job_identity
        replacing={job_identity(Job.model_validate(j)) for state in resume_states.values() for j in state['jobs']}
        prior_jobs=[Job.model_validate(j) for j in checkpoint['jobs'] if job_identity(Job.model_validate(j)) not in replacing]
        plan={'budget_used':checkpoint['budget_used'] if resume_budget=='remaining' else {},
              'query_history':checkpoint.get('query_history',[]) if resume_budget=='remaining' else []}
        discovery.update(status='resume_traversal',resume_budget_mode=resume_budget,prior_progress=str(resume_file),
                         coverage='只继续未完成遍历；已读原文证据沿用原时间，不冒充本次重新读取')
    if operation == "new_search":
        if not candidates_file or not plan_file:
            raise ValueError("new-search需要本次计划与实际搜索凭据；Python不能调用宿主 agent 的会话搜索")
        receipt=json.loads(local_path(candidates_file).read_text(encoding="utf-8-sig"))
        plan=json.loads(local_path(plan_file).read_text(encoding="utf-8-sig"))
        discovery["search_log"]=validate_receipt(config,plan,receipt)
        ensure_new_execution(plan,discovery['search_log'],ROOT/'outputs'/'real')
        discovery['entry_tasks'] = plan.get('entry_tasks',[])
        for task in plan.get('entry_tasks',[]):
            if task['status'] == 'planned':
                candidates.append(Candidate.model_validate(task['candidate']))
        discovery.update(status="new_queries_executed" if any(x.get("status")=="success" for x in discovery["search_log"]) else "search_incomplete",plan_id=plan["plan_id"],search_mode=receipt.get("search_mode","not_exposed"))
    else:
        discovery["historical_search_log"]=discovery.pop("search_log",[])
        discovery["search_log"]=[]
        if operation == "refresh":
            discovery.update(status="refresh_known_candidates",coverage="重新读取已知候选；未执行新搜索，未扩大覆盖")
    discovery.update(operation=operation,config_fingerprint=fingerprint(config),effective_config=effective,live_refetched=False)
    discovery['input_artifacts'] = {label:dict(path=str(local_path(path)),sha256=hashlib.sha256(local_path(path).read_bytes()).hexdigest())
        for label,path in [('candidates',candidates_file),('plan',plan_file),('policies',policies_file)] if path}
    discovery["candidates"]=[c.model_dump(mode="json") for c in candidates]
    if mode=="real" and any(c.synthetic for c in candidates):
        raise ValueError("合成候选不能进入真实运行")
    if mode=="test" and any(not c.synthetic for c in candidates):
        raise ValueError("离线样例只允许明确标记的合成候选")
    store=ResourceStore(resource_path) if resource_path is not None and mode=="real" else None
    if store:
        resource_ids=record_candidates(store,candidates)
        resource_ids=list(dict.fromkeys(resource_ids+record_task_companies(store,discovery.get('search_log',[]))))
        store.snapshot()
        discovery['company_library']={'path':str(store.path),'discovered_company_ids':resource_ids}
    policies=json.loads(local_path(policies_file).read_text(encoding="utf-8")) if policies_file else {}
    targets=cities(config)
    prior_budget = plan.get('budget_used',{})
    robots_cache = RobotsCache(Path(resource_path).parent/'network-cache' if resource_path is not None and mode=='real' else None)
    fetchers={c:Fetcher(config.budget.model_copy(update={"requests":config.budget.city_requests(i,len(targets),prior_budget.get('requests',{}).get(c,0))}),policies,output/"evidence") for i,c in enumerate(targets)}
    shared_last={}
    shared_intervals={}
    stopped_hosts=dict(checkpoint.get('stopped_hosts',{})) if resume_file and resume_budget=='remaining' else {}
    for client in fetchers.values():
        client.last=shared_last
        client.stopped_hosts=stopped_hosts
        client.robots_cache=robots_cache
        client.required_intervals=shared_intervals
    company_sets={c:{name_key(n):n for n in prior_budget.get('companies',{}).get(c,[])} for c in targets}
    read_memo, fallback_requests = {}, []
    jobs=list(prior_jobs)
    reports=[]
    companies=[]
    if catalog_file:
        companies=json.loads(local_path(catalog_file).read_text(encoding="utf-8"))["companies"]
    seen_channels=set()
    traversals=dict(checkpoint['states']) if resume_file else {}
    visited_entries=set(checkpoint.get('visited_entries',[])) if resume_file else set()
    for candidate in candidates:
        if not candidate.local_file and candidate.source_channel in config.sources and candidate.source_channel not in ('boss','linkedin','bendibao_wechat') and candidate.candidate_type!='enterprise_directory' and (not candidate.target_cities or set(candidate.target_cities).intersection(targets)):
            traversals.setdefault(scope_id(candidate),initial_state(candidate,'entry_not_started'))

    def usage():
        return dict(requests={c:prior_budget.get('requests',{}).get(c,0)+f.requests for c,f in fetchers.items()},
                    searches={c:prior_budget.get('searches',{}).get(c,0)+sum(q.get('target_city')==c and q.get('status') in ('success','failed','restricted') for q in discovery.get('search_log',[])) for c in targets},
                    companies={c:sorted(v.values()) for c,v in company_sets.items()},
                    company_counts={c:len(v) for c,v in company_sets.items()})

    def save_progress(stage):
        for r in reports:
            if r.get('resolved_home_scope_id'):
                traversals.pop(r['resolved_home_scope_id'],None)
            for pending in r.get('entry_pending_states',[]):
                traversals.setdefault(pending['scope_id'],pending)
        traversals.update({r['traversal']['scope_id']:r['traversal'] for r in reports if r.get('traversal')})
        discovery['budget_used']=usage()
        output.mkdir(parents=True,exist_ok=True)
        attempts=[a | {'budget_city':c} for c,f in fetchers.items() for a in f.read_attempts]
        events=[e | {'budget_city':c} for c,f in fetchers.items() for e in f.events]
        audit,processing,continuations=build_execution_audit(candidates,reports,traversals,attempts,events,discovery,
            str(output/'traversal-progress.json') if traversals else None)
        audit['stage']=stage
        discovery.update(core_execution=audit,company_processing=processing,continuation_requests=continuations,
                         read_attempts=attempts)
        for name,value in [('core-execution.json',audit),('company-processing.json',processing),('continuation-requests.json',continuations)]:
            temporary=output/(name+'.tmp')
            temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
            temporary.replace(output/name)
        (output/'read-attempts.jsonl').write_text(''.join(json.dumps(a,ensure_ascii=False)+'\n' for a in attempts),encoding='utf-8')
        if not traversals:
            return
        saved_jobs=deduplicate([Job.model_validate(j) for s in traversals.values() for j in s['jobs']]+jobs)
        progress=dict(schema_version='traversal-run-1',config_fingerprint=fingerprint(config),
            states=traversals,budget_used=usage(),jobs=[j.model_dump(mode='json') for j in saved_jobs],
            updated_at=now(),stage=stage,complete=all(s['complete'] for s in traversals.values()),
            visited_entries=sorted(visited_entries),stopped_hosts=dict(stopped_hosts),
            query_history=list(dict.fromkeys(plan.get('query_history',[])+[q['query'] for q in discovery.get('search_log',[]) if q.get('status') in ('success','failed','restricted')])),
            network_events=(checkpoint.get('network_events',[]) if resume_file else [])+[e | {'budget_city':c} for c,f in fetchers.items() for e in f.events],
            search_log=(checkpoint.get('search_log',[]) if resume_file else [])+discovery.get('search_log',[]),
            resume_budget_mode=resume_budget if resume_file else 'initial')
        from .spec import spec_for_config
        progress.update(rules_version=config.rules_version,effective_search_spec=spec_for_config(config).model_dump(mode='json'),raw_input=(effective or {}).get('raw_input',config._raw_input))
        discovery['traversal_progress']=progress
        output.mkdir(parents=True,exist_ok=True)
        temporary=output/'traversal-progress.json.tmp'
        temporary.write_text(json.dumps(progress,ensure_ascii=False,indent=2),encoding='utf-8')
        temporary.replace(output/'traversal-progress.json')

    def process_candidate(candidate):
        employer_pages=[]
        owner=next((c for c in candidate.target_cities if c in targets),targets[0])
        fetcher=SharedReader(fetchers[owner],read_memo)
        seen_channels.add(candidate.source_channel)
        report={"channel":candidate.source_channel,"url":candidate.url,"access_status":"not_run","parse_status":"not_run","jobs_extracted":None,"reason":""}
        checklist={"legal_name":candidate.company,"official_url":candidate.official_url,"career_url":candidate.career_url,"source_url":candidate.url,"version_date":candidate.discovered_at[:10],"discovery_kind":"unknown","verification_status":"pending","notes":"搜索发现的主体/网址仍需原文与导航核实；不凭标题认定官方"}
        report.update(company=candidate.company,target_cities=candidate.target_cities,task_ids=candidate.task_ids,entry_task_id=candidate.entry_task_id,candidate_type=candidate.candidate_type,attempted=False)
        reports.append(report)
        try:
            if candidate.candidate_type == 'enterprise_directory':
                raise FetchError('not_run','企业目录只用于企业发现；通过import-directory/import-companies导入，不作为岗位详情读取')
            if candidate.target_cities and not set(candidate.target_cities).intersection(targets):
                raise FetchError("not_run","候选发现城市与本次目标不交集，未检查不等于无岗位")
            if candidate.company:
                if name_key(candidate.company) not in company_sets[owner] and config.budget.companies_per_city is not None and len(company_sets[owner]) >= config.budget.companies_per_city:
                    raise FetchError("budget_exhausted","本城市企业检查预算耗尽")
                company_sets[owner].setdefault(name_key(candidate.company),candidate.company)
            if candidate.source_channel not in config.sources:
                raise FetchError("not_run","渠道未启用")
            if candidate.local_file:
                if candidate.synthetic != (mode=="test"):
                    raise ValueError("导入数据模式不匹配")
                extracted=import_jobs(candidate)
                report.update(access_status="manual_import",parse_status="success")
                checklist["verification_status"]="manual_import_not_online_verified"
            else:
                if mode=="test":
                    raise ValueError("测试模式禁止联网")
                if candidate.source_channel in ("boss", "linkedin", "bendibao_wechat"):
                    raise FetchError("requires_authorization","此渠道没有可用授权自动接口，只支持候选链接记录和有权提供的材料")
                if candidate.source_channel == 'official_career':
                    candidate,page,employer_pages,extracted = yield from read_entry_steps(fetcher,candidate,config,store,reports,report,resume_states,visited_entries)
                    checklist.update(verification_status='source_classified_no_identity_gate',
                                     career_url=report.get('resolved_career_url') or candidate.career_url,
                                     notes=json.dumps(report.get('entry_trace',[]),ensure_ascii=False))
                    if report.get('fallback_search_reason') and candidate.company:
                        for city in report.get('fallback_target_cities',candidate.target_cities or [owner]):
                            if city in targets:
                                fallback_requests.append(dict(company=candidate.company,target_city=city,
                                    source_url=candidate.url,reason=report['fallback_search_reason']))
                elif resume_file and scope_id(candidate) in resume_states and not resume_states[scope_id(candidate)].get('entry_pending'):
                    page=None
                    extracted=yield from extract_page_steps(fetcher,candidate,None,reports,report,targets=targets,config=config,state=resume_states[scope_id(candidate)])
                else:
                    public_notice=candidate.source_channel=='public_recruitment'
                    publisher_policy=policies.get(urlsplit(candidate.url).hostname,{})
                    if public_notice and not (publisher_policy.get('publisher_verified') and publisher_policy.get('publisher_evidence_url')):
                        raise FetchError('policy_pending','公开招聘转载来源发布方尚未审核；不当作企业官网')
                    if not public_notice and not candidate.official_url:
                        raise FetchError("official_unverified","缺少官网来源，不能认定官方入口")
                    report["attempted"]=True
                    if candidate.announcement_url and not public_notice:
                        report['announcement_evidence']=verify_announcement_employer(fetcher,candidate,store)
                    if public_notice and candidate.candidate_type=='announcement' and store:
                        notice_companies,home=read_announcement(fetcher,candidate.url,store)
                        report['discovered_employers']=notice_companies
                    else:
                        home=fetcher.get(candidate.url if public_notice else candidate.official_url)
                    if not public_notice and candidate.company:
                        identity=record_profile(store,candidate.company,home.get('final_url',candidate.official_url),home,candidate.official_url)
                        report['company_identity_status']='name_on_original_home' if identity else 'pending'
                        if candidate.announcement_url and not identity:
                            raise FetchError('employer_unverified','公告企业全称未在官网原文核实；品牌与法律主体关系仍待确认')
                    employer_pages.append((candidate.url if public_notice else candidate.official_url,home))
                    home_links=links(home["data"],home.get("final_url",candidate.url if public_notice else candidate.official_url))
                    if not public_notice and not candidate.career_url:
                        choices=discover_career(fetcher,candidate,home)
                        report['discovered_links']=[x | {'status':'entry_available' if x['approved'] else 'policy_pending','parent_url':candidate.official_url} for x in choices]
                        approved=[x for x in choices if x['approved']]
                        if not approved:
                            raise FetchError('policy_pending' if choices else 'navigation_missing','官网已读取；招聘导航待审核' if choices else '官网已读取，但未找到明确招聘导航；不代表无岗位')
                        selected=approved[0]['url']
                        candidate=candidate.model_copy(update={'career_url':selected,'url':selected if candidate.candidate_type=='company_home' else candidate.url,'candidate_type':'career_entry' if candidate.candidate_type=='company_home' else candidate.candidate_type,'original_discovered_url':candidate.url})
                        checklist['career_url']=selected
                        report['resolved_career_url']=selected
                    if candidate.company_profile_url:
                        if not any(canonical(a['url'])==canonical(candidate.company_profile_url) for a in home_links):
                            raise FetchError('official_unverified','企业简介页未在已读取官网导航中明确关联')
                        profile_page=fetcher.get(candidate.company_profile_url)
                        employer_pages.append((candidate.company_profile_url,profile_page))
                        if not public_notice and candidate.company:
                            record_profile(store,candidate.company,candidate.company_profile_url,profile_page)
                    nav={'publisher_evidence':publisher_policy.get('publisher_evidence_url'),'employer_official':False} if public_notice else verify_navigation(fetcher,candidate,home)
                    if store and not public_notice and candidate.company and report.get('company_identity_status')=='name_on_original_home':
                        store.add(candidate.company,candidate.official_url,home['retrieved_at'],'official_navigation',
                                  values={'career_url':candidate.career_url},supported=('career_url',),
                                  evidence={'quote':json.dumps(nav,ensure_ascii=False),'content_hash':home['hash'],'snapshot':home['snapshot']})
                    careers=home if public_notice else fetcher.get(candidate.career_url)
                    if not public_notice and candidate.company:
                        record_profile(store,candidate.company,candidate.career_url,careers)
                    career_links=links(careers["data"],candidate.url if public_notice else candidate.career_url)
                    detail_link=[a for a in career_links if canonical(a["url"])==canonical(candidate.url)]
                    if public_notice or canonical(candidate.url)==canonical(candidate.career_url):
                        detail_link=[{"url":candidate.url,"status":"入口内含公告；仍须逐岗位提取"}]
                    page=fetcher.get(candidate.url)
                    employer_pages.append((candidate.url,page))
                    if not detail_link:
                        # A surviving first-party archived detail may no longer be listed.
                        # Require its own backlink to the already verified career navigation.
                        # This proves origin, NOT that the vacancy is still open.
                        cp, dp = urlsplit(candidate.career_url), urlsplit(candidate.url)
                        backlinks=[a for a in links(page["data"],candidate.url) if a["url"].rstrip("/")==candidate.career_url.rstrip("/")]
                        if cp.hostname!=dp.hostname or not dp.path.startswith(cp.path.rstrip("/")+"/") or not backlinks:
                            raise FetchError("official_unverified","招聘入口与候选详情没有可核实的导航/同站路径反向关联")
                        detail_link=[{"reverse_link":backlinks,"status":"原站历史详情；当前列表未确认，开放状态未知"}]
                    checklist.update(verification_status="public_publisher_not_employer_verified" if public_notice else "official_navigation_verified",notes=json.dumps({"home":candidate.official_url,"home_hash":home["hash"],"navigation":nav,"career_hash":careers["hash"],"detail_link":detail_link},ensure_ascii=False))
                    if not public_notice and candidate.company:
                        record_profile(store,candidate.company,candidate.url,page)
                    report["access_status"]="success"
                    report.update(content_read_at=page["retrieved_at"],validity_checked_at=page["retrieved_at"])
                    if candidate.candidate_type=="company_home":
                        raise FetchError("not_run","企业主页不能作为具体岗位")
                    extracted=yield from extract_page_steps(fetcher,candidate,page,reports,report,targets=targets,config=config)
            for j in extracted:
                if j.source_type == 'unknown' and candidate.source_channel != 'official_career':
                    from .entries import source_type
                    j.source_type = source_type(candidate)
                if candidate.source_channel=='public_recruitment':
                    j.notes.append('公开招聘发布方原文，非企业官网；招聘主体与当前岗位有效性需复核')
                for source_url, employer_page in employer_pages:
                    category_evidence=scope_evidence(employer_page,source_url,j.company,config.company_scope_keywords)
                    if category_evidence:
                        j.evidence.append(category_evidence)
                j.discovery_task_ids=list(dict.fromkeys(j.discovery_task_ids+candidate.task_ids))
                j.discovery_cities=list(dict.fromkeys(j.discovery_cities+candidate.target_cities))
                if not candidate.local_file:
                    j.validity_checked_at=j.validity_checked_at or (page["retrieved_at"] if page else None)
                if candidate.company and j.company!=candidate.company:
                    j.notes.append("候选企业名称与原始招聘主体名称不同，以原文为准，目录关联待确认")
            if store:
                for j in extracted:
                    store.add(j.company,j.source_url,j.evidence[0].retrieved_at if j.evidence else now(),'job_original',
                              context={'title':j.title,'source_channel':j.source_channel,'candidate_company':candidate.company})
                    if j.evidence and not j.synthetic:
                        evidence = next((e for e in j.evidence if e.method not in ('search_snippet','fixture')), None)
                        if evidence and evidence.field_quotes.get('company') == j.company and evidence.field_quotes.get('title') == j.title:
                            work_cities = list(dict.fromkeys(r.geo.city for r in evaluate(j,config) if r.geo.state=='PASS' and r.geo.city))
                            store.add(j.company,evidence.source_url,evidence.retrieved_at,'job_original',
                                      values={'hiring_history':[{'title':j.title,'cities':work_cities}]},supported=('hiring_history',),
                                      evidence={'quote':json.dumps(evidence.field_quotes,ensure_ascii=False),
                                                'content_hash':evidence.content_hash,'entity_name':j.company,
                                                'kind':'job','subject_relation':'self'})
            jobs.extend(extracted)
            if report.get('parse_status') == 'success' and not report.get('adapter') and not report.get('entry_trace'):
                report.update(jobs_extracted=len(extracted),reason="原始内容已提取；岗位仍开放及求职条件另行判断")
        except FetchError as exc:
            report.update(access_status=exc.status,reason=str(exc))
            if exc.status=='budget_exhausted' and candidate.source_channel=='official_career' and not candidate.local_file and not report.get('traversal'):
                report.update(traversal=initial_state(candidate,exc.status),coverage_incomplete=True)
        except (ParseError, ValueError, OSError) as exc:
            report.update(parse_status="failed",reason=str(exc))
        if candidate.company and not any(c.get("legal_name")==candidate.company for c in companies):
            companies.append(checklist)
        else:
            for c in companies:
                if c.get("legal_name")==candidate.company:
                    c.update({k:v for k,v in checklist.items() if k in ("verification_status","notes")})
    lanes={c:deque() for c in targets}
    scheduled=set()
    for candidate in candidates:
        key=scope_id(candidate)
        if key in scheduled:
            continue
        scheduled.add(key)
        owner=next((c for c in candidate.target_cities if c in targets),targets[0])
        lanes[owner].append(process_candidate(candidate))
    save_progress('started')
    try:
        while any(lanes.values()):
            for city in targets:
                if not lanes[city]:
                    continue
                step=lanes[city].popleft()
                try:
                    next(step)
                except StopIteration:
                    pass
                else:
                    lanes[city].append(step)
                save_progress('reading')
    except BaseException:
        save_progress('interrupted')
        raise
    save_progress('finished')
    for channel, reason in [("boss","无已配置授权自动检索；可记录候选和人工导入"),("linkedin","未经许可不自动抓取；可人工导入"),("bendibao_web","本轮未取得逐站点自动访问依据；不因一次失败认定永久不可用"),("bendibao_wechat","公众号自动获取未接入；可有权导入")]:
        if channel not in seen_channels:
            reports.append(dict(channel=channel,url=None,access_status="not_run",parse_status="not_run",jobs_extracted=None,reason=reason))
    if not candidates:
        reports.append(dict(channel="search",url=None,access_status=discovery["status"],parse_status="not_run",jobs_extracted=None,reason=discovery["coverage"]))
    jobs=deduplicate(jobs)
    results=[r for j in jobs for r in evaluate(j,config)]
    events=[e | {"budget_city":c} for c,f in fetchers.items() for e in f.events]
    discovery["live_refetched"]=any(e.get('status')=='success' and is_content_event(e) for e in events)
    discovery["budget_allocation"]={c:f.budget.requests for c,f in fetchers.items()}
    if mode == 'real' and fallback_requests:
        discovery['follow_up_plan'] = followup_plan(config,fallback_requests,discovery.get('search_log',[]),plan,usage())
    discovery['entry_log'] = []
    for task in discovery.get('entry_tasks',[]):
        checked = next((r for r in reports if r.get('entry_task_id') == task['task_id']),{})
        discovery['entry_log'].append(dict(task_id=task['task_id'],company=task['company'],target_city=task['target_city'],
            status=checked.get('entry_outcome',checked.get('access_status',task['status'])),
            reason=checked.get('fallback_search_reason') or checked.get('reason') or task['reason']))
    discovery["execution_status"]="partial" if any(r.get("coverage_incomplete") or r.get("access_status") not in ("success","manual_import","cached_progress") or r.get("parse_status") not in ('success','listing','navigation') or any(x.get('status') in ('policy_pending','budget_exhausted') for x in r.get('discovered_links',[])) for r in reports if r.get("url")) or any(q.get("status")!="success" for q in discovery.get("search_log",[])) or any(t['status']!='relevant_job_found' for t in discovery['entry_log']) else "completed_within_declared_scope"
    for record in discovery["candidates"]:
        report=next((r for r in reports if r.get("url")==record["url"]),None)
        record["processing_status"]=f"{report['access_status']}/{report['parse_status']}" if report else "not_run"
    if store:
        snapshot=store.snapshot()
        discovery['company_library']['total_companies']=len(snapshot['companies'])
    return export_bundle(output,config,jobs,results,reports,companies,discovery,events,mode)


def reexport(config, existing, output, effective=None):
    old=json.loads(local_path(existing).read_text(encoding="utf-8"))
    mode=old.get("mode","real")
    output=output_path(output,mode)
    jobs=deduplicate([Job.model_validate(j) for j in old["jobs"]])
    # Ignore ALL old results, cached MATCH flags and old geography rule versions.
    results=[r for j in jobs for r in evaluate(j,config)]
    discovery=old.get("discovery",{}) | {"provider":"cache_revalidation","status":"revalidated_not_refetched","origin_provider":old.get("discovery",{}).get("provider"),"coverage":"本次仅对历史原文证据执行当前规则，未再次联网读取；原始覆盖："+old.get("discovery",{}).get("coverage","未记录"),"cache_revalidated_at":now(),"live_refetched":False}
    discovery.update(operation="reexport",config_fingerprint=fingerprint(config),historical_search_log=discovery.get("search_log") or discovery.get("historical_search_log",[]),search_log=[])
    discovery["effective_config"]=effective
    discovery['historical_entry_log'] = discovery.pop('entry_log',[])
    discovery['historical_follow_up_plan'] = discovery.pop('follow_up_plan',None)
    discovery['entry_tasks'] = []
    discovery['historical_traversal_progress']=discovery.pop('traversal_progress',None)
    for key in ('core_execution','company_processing','continuation_requests','read_attempts','input_artifacts'):
        if key in discovery:
            discovery['historical_'+key]=discovery.pop(key)
    reports=[r | {"historical_access_status":r.get("historical_access_status",r.get("access_status")),"historical_parse_status":r.get("historical_parse_status",r.get("parse_status")),"historical_jobs_extracted":r.get("historical_jobs_extracted",r.get("jobs_extracted")),"historical_reason":r.get("historical_reason",r.get("reason")),"jobs_extracted":None,"access_status":"not_run","parse_status":"not_run","attempted":False,"reason":"本次仅重新导出；旧状态/时间保留在历史字段"} for r in old.get("reports",[])]
    return export_bundle(output,config,jobs,results,reports,old.get("companies",[]),discovery,events=old.get("network_events",[]),mode=mode)
