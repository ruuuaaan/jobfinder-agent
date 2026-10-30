from __future__ import annotations
import hashlib
import json
import time
from urllib.parse import urlsplit
from jobfinder.models import Job, SearchConfig, now
from jobfinder.matching import evaluate, deduplicate
from jobfinder.resources import ResourceStore
from .storage import Store, ROOT
from .domain import available_query, schedule_index_fallback
from .providers import SearchProvider, ModelProvider, ModelOutputIssue, read_public, extract_jobs, AccessIssue
from .browser import Browser, SearchNeedsHelp
from .search_routes import NativeSearch, task_mode, resolve_mode, token_usage
from .recovery import classify_issue, terminal_status, retry_time, ready, migrate_task
from .discovery import qualify_candidate, link_decision, SELECTION_VERSION
from .batches import plan as plan_batches, split as split_batch, progress as batch_progress
from .quality import merge_jobs
from .page_signals import page_readiness
from .search_execution import bind_providers, choices, begin_attempt, finish_attempt, classify_failure, drain_reason, short_retry_wait
from .site_access import requires_session, site_key
from .site_flow import access_for, refresh_sites, candidate_available, provider_url, note_site_issue, waiting_sites, retry_timestamp, resume_task_site


def error_text(exc):
    if isinstance(exc, (AccessIssue, ValueError)):
        return str(exc)[:320]
    return '操作失败（'+type(exc).__name__+'），已保留进度，可重试或检查连接'


def reserve_search(store, tid):
    selected = []
    def reserve(t):
        if t['state']!='running':
            return
        if 'search_provider_chain' not in t:bind_providers(store,t)
        t['site_access_state']=access_for(store).snapshot()
        q = available_query(t)
        if q:
            route=choices(t,q)[0]
            begin_attempt(t,q,route)
            selected.append(dict(q))
        elif t['usage']['model_calls']>=t['input']['model_call_limit']:t['state']='limit_reached'
    store.update(tid,reserve)
    return selected[0] if selected else None


def add_candidates(task, rows, query, parent=None):
    known = {c['url'] for c in task['candidates']}
    for item in rows:
        url = item.get('url','').split('#')[0]
        parts=urlsplit(url)
        if parts.scheme not in ('http','https') or not parts.hostname or parts.username or parts.password:
            task.setdefault('rejected_links',[]).append({'url':url,'reason':'不支持或含凭据的链接','query_id':query['id']})
            continue
        if url in known:
            continue
        known.add(url)
        task['candidates'].append({'id':hashlib.sha256(url.encode()).hexdigest()[:20], 'url':url,
            'title':str(item.get('title') or item.get('text') or url)[:200],
            'summary':str(item.get('content') or item.get('summary') or '')[:2000], 'channel':query['channel'], 'city':query['city'],
            'query_id':query['id'], 'status':'pending', 'discovered_at':now(), 'parent_url':parent,
            'depth':query.get('depth',-1)+1,'search_route':query.get('search_route'),
            'discovery_method':query.get('method'),'original_url':url,
            'preferred_reader':query.get('preferred_reader','public'),
            'url_resolution':{'state':'https_probe_required' if parts.scheme=='http' else 'original_https'}})
        qualify_candidate(task,task['candidates'][-1])


def search_receipt(store,tid,query,response):
    folder=store.path/'evidence'/tid/'searches'
    folder.mkdir(parents=True,exist_ok=True)
    path=folder/(query['id']+'-attempt-'+str(len(query.get('attempts',[])) or 1)+'.json')
    path.write_text(json.dumps({'query':query['query'],'search_route':query['search_route'],
                               'retrieved_at':now(),**response},ensure_ascii=False,indent=2),encoding='utf-8')
    return str(path)


def enqueue_leads(store,tid,query,response,receipt):
    """Pack observed cards/snippets without cross-record attribution or new searches."""
    from .batches import span
    rows=response.get('results',[])
    records=[];text='';batches=[];start=0
    for row in rows:
        content=str(row.get('content') or row.get('summary') or '')
        if not content:continue
        import re
        block=re.sub(r'\s+',' ',str(row.get('title') or '')+' '+content).strip()
        if len(text)-start+len(block)>1600 and len(text)>start:
            batches.append(span(start,len(text)));start=len(text)
        records.append({'url':row['url'],'text':block})
        text+=block+'\n\n'
    if not records:return
    batches.append(span(start,len(text)))
    tier='onsite_list' if query['search_route']=='onsite' else 'search_snippet'
    page={'url':records[0]['url'],'title':query['query']+' · '+('站内列表' if tier=='onsite_list' else '搜索摘要'),
        'text':text,'records':records,'links':[{'url':r['url'],'text':r['text']} for r in records],
        'method':tier,'evidence_level':tier,'retrieved_at':now(),'search_receipt':receipt,
        'content_hash':hashlib.sha256(text.encode()).hexdigest(),'planned_batches':batches}
    folder=store.path/'evidence'/tid;folder.mkdir(parents=True,exist_ok=True)
    path=folder/(page['content_hash']+'.json');page['snapshot']=str(path)
    path.write_text(json.dumps(page,ensure_ascii=False,indent=2),encoding='utf-8')
    def add(t):
        t['candidates'].append({'id':'leads-'+query['id'],'url':page['url'],'title':page['title'],'channel':query['channel'],
            'city':query['city'],'query_id':query['id'],'status':'pending_extract','page_snapshot':str(path),
            'evidence_packet':True,'links_expanded':True,'depth':0,'discovered_at':now(),
            'selection':{'eligible':True,'priority':1,'reason':'保留摘要或列表线索，不能冒充详情'}})
    store.update(tid,add)


def seed_resource_entries(store, task):
    if task.get('resource_entries_selected') or 'official_career' not in task['input']['channels']:
        return
    task['resource_entries_selected']=True
    count=0
    if task.get('execution_policy_version',0)>=2:
        from .resource_reuse import successful_details
        for entry in successful_details(store.all(),task['input']):
            before=len(task['candidates'])
            add_candidates(task,[{'url':entry['url'],'title':entry['company']+' · '+entry['title']}],
                {'id':'resource_reuse','channel':'official_career','city':entry['city']})
            if len(task['candidates'])>before:
                count+=1
                task['candidates'][-1].update(resource_provenance=entry,preferred_reader=entry['preferred_reader'],
                    verified_company=entry['company'],verified_official_url=entry['url'])
                task['candidates'][-1]['selection']['priority']=0
    for path in (ROOT/'data/resources/companies.sqlite3',store.path/'companies.sqlite3'):
        if not path.exists():
            continue
        from .resource_reuse import related_entries
        for company in ResourceStore.read_existing(path):
            for entry in related_entries(company,task['input']):
                before=len(task['candidates'])
                add_candidates(task,[{'url':entry['url'],'title':company['company_name']+' · 历史招聘入口待复核'}],
                    {'id':'resource_reuse','channel':'official_career','city':entry['city']})
                if len(task['candidates'])>before:
                    count+=1
                    task['candidates'][-1].update(resource_provenance=entry,preferred_reader=entry.get('preferred_reader','public'))
                    task['candidates'][-1]['selection']['priority']=0 if entry.get('preferred_reader')=='browser' else 1
                    if company.get('field_status',{}).get('official_url')=='source_supported':
                        task['candidates'][-1].update(verified_company=company['company_name'],verified_official_url=company.get('official_url'))
    if count:
        task['events'].append({'at':now(),'message':f'按历史查询中的岗位与城市选取 {count} 个相关企业入口；将重新读取原文，不沿用旧在招结论','kind':'info'})


def save_page(store, tid, candidate, page):
    folder = store.path/'evidence'/tid
    folder.mkdir(parents=True,exist_ok=True)
    name = page['content_hash']+'.json'
    if not (folder/name).exists():
        (folder/name).write_text(json.dumps(page,ensure_ascii=False,indent=2),encoding='utf-8')
    page['snapshot'] = str(folder/name)
    def update(t):
        c = next(c for c in t['candidates'] if c['id']==candidate['id'])
        c.update(page_snapshot=page['snapshot'],read_at=page['retrieved_at'], method=page['method'])
    store.update(tid,update)


def process_page(store, tid, candidate, page):
    if not candidate.get('evidence_packet'):
        page['readiness']=page_readiness(page)
        if page['readiness']['state']=='loading_shell':
            raise AccessIssue('已保存页面仍只有列表框架，保留原文并等待重新读取','dynamic_unreadable',
                evidence={'url':page['url'],'content_hash':page['content_hash'],'readiness':page['readiness']})
    if not candidate.get('page_snapshot'):
        save_page(store,tid,candidate,page)
    page.setdefault('snapshot',candidate.get('page_snapshot'))
    if not candidate.get('evidence_packet') and page['readiness']['state']=='vacancy_closed':
        def closed(t):
            c=next(c for c in t['candidates'] if c['id']==candidate['id'])
            c.update(status='read',error=None,issue=None,outcome='vacancy_closed',links_expanded=True,
                     closed_page={k:page[k] for k in ('url','retrieved_at','content_hash','snapshot')})
            c['closed_page'].update(quote=page['readiness']['quote'],urls=list(dict.fromkeys([c['url'],page['url'],page.get('requested_url',c['url'])])))
            from .closed_vacancies import apply_closed_evidence
            apply_closed_evidence(t)
            t['events'].append({'at':now(),'kind':'info','message':'原页明确岗位已过期/不存在，已记录证据；跳过模型与推荐页：'+c['title'][:70]})
        store.update(tid,closed)
        return
    reserved=[]
    def reserve(t):
        c=next(c for c in t['candidates'] if c['id']==candidate['id'])
        if 'batches' not in c:
            c['batches']=page.get('planned_batches') or plan_batches(page['text'],size=3500,overlap=500)
            c['batch_source_hash']=page['content_hash']
        if c['batch_source_hash']!=page['content_hash']:
            raise AccessIssue('原文已变化，不能沿用旧批次','source_changed')
        if not c.get('links_expanded') and not c.get('evidence_packet'):
            # Evaluate the actual target and label together. A keyword-only
            # prefilter hid generic detail buttons and subsidiary career URLs.
            links=[x for x in page['links'] if link_decision(t,x,page['url'])[0]]
            if c.get('depth',0)<4:
                add_candidates(t,links,{'id':c['query_id'],'channel':c['channel'],'city':c['city'],'depth':c.get('depth',0),
                    'preferred_reader':'browser' if page['method']=='local_edge_visible_dom' else 'public'},page['url'])
            c['links_expanded']=True
        if not c.get('evidence_packet'):
            from .discovery import extraction_decision
            eligible,reason=extraction_decision(page)
            if not eligible:
                c.update(status='read',outcome='non_vacancy',extraction_skipped=reason)
                for b in c['batches']:
                    if b['status']=='pending':b.update(status='skipped',reason=reason)
                t['events'].append({'at':now(),'kind':'info','message':reason+'：'+c['title'][:60]})
                return
        c['status']='pending_extract'
        batch=next((b for b in c['batches'] if b['status']=='pending'),None)
        if not batch:return
        if t['state']=='running' and t['usage']['model_calls']<t['input']['model_call_limit']:
            t['usage']['model_calls']+=1
            from .scheduling import batch_calls
            c['model_attempts']=batch_calls(c)+1
            batch.update(status='running',started_at=now())
            reserved.append(dict(batch))
        elif t['state']=='running':t['state']='limit_reached'
    task=store.update(tid,reserve)
    if not reserved:return
    batch=reserved[0]
    try:
        cfg=store.connection(cid=task['connections']['model'],reveal=True)
        if not cfg:raise AccessIssue('本任务模型连接已移除','model_connection')
        jobs,usage=extract_jobs(page,candidate,task,ModelProvider(cfg),batch=batch)
    except Exception as exc:
        issue=classify_issue(exc)
        usage=token_usage(getattr(exc,'usage',{}))
        def failed(t):
            c=next(c for c in t['candidates'] if c['id']==candidate['id'])
            b=next(b for b in c['batches'] if b['id']==batch['id'])
            b.update(status='failed',issue=issue,usage=usage,completed_at=now())
            children=[]
            if issue['kind']=='model_truncated' and page.get('records'):
                positions=[];offset=0
                for r in page['records']:
                    pos=page['text'].find(r['text'],offset);offset=pos+len(r['text'])
                    if b['start']<pos<b['end']:positions.append(pos)
                if positions:
                    from .batches import span
                    middle=min(positions,key=lambda p:abs(p-(b['start']+b['end'])/2))
                    children=[span(b['start'],middle,b['id']),span(middle,b['end'],b['id'])]
            elif issue['kind']=='model_truncated':children=split_batch(b)
            if children:
                b['status']='split';c['batches'].extend(children)
            c.setdefault('issue_history',[]).append(issue)
            c.update(issue=issue,error=issue['message'],status='pending_extract' if any(b['status']=='pending' for b in c['batches']) else 'system_error')
            t['usage']['input_tokens']+=usage['input_tokens'];t['usage']['output_tokens']+=usage['output_tokens']
            t['events'].append({'at':now(),'kind':'warning','message':('当前批次已拆小，将在剩余额度内继续：' if children else '本批整理异常已保存，继续其他批次／页面：')+c['title'][:60]})
        store.update(tid,failed)
        return
    def finish(t):
        c = next(c for c in t['candidates'] if c['id']==candidate['id'])
        b=next(b for b in c['batches'] if b['id']==batch['id'])
        b.update(status='done',completed_at=now(),extracted_count=len(jobs),usage=token_usage(usage))
        more=any(b['status']=='pending' for b in c['batches'])
        failed=any(b['status']=='failed' for b in c['batches'])
        c.update(status='pending_extract' if more else 'system_error' if failed else 'read',extracted_count=c.get('extracted_count',0)+len(jobs))
        if not failed:c.update(error=None,issue=None)
        t['usage']['input_tokens'] += usage.get('prompt_tokens',0) or 0
        t['usage']['output_tokens'] += usage.get('completion_tokens',0) or 0
        c.setdefault('rejected_records',[])
        c['rejected_records'].extend(r for r in page.get('rejected_records',[]) if r not in c['rejected_records'])
        c.setdefault('extraction_checks',[]).append({'batch_id':batch['id'],**page.get('extraction_check',{})})
        all_jobs = merge_jobs([Job.model_validate(j) for j in t['jobs']] + jobs)
        t['jobs'] = [j.model_dump(mode='json') for j in all_jobs]
        from .closed_vacancies import apply_closed_evidence
        apply_closed_evidence(t)
        p=batch_progress(c)
        t['events'].append({'at':now(),'message':f'分批整理 {p["done"]}/{p["total"]}，本批保存 {len(jobs)} 条岗位：'+page['title'][:70],'kind':'success' if jobs else 'info'})
    store.update(tid,finish)
    resources = ResourceStore(store.path/'companies.sqlite3')
    for company in page.get('company_facts',[]):
        resources.add(company['company_name'],page['url'],page['retrieved_at'],'web_original_company',values=company['values'],supported=company['values'].keys(),
            evidence={'quote':company['quote'],'content_hash':page['content_hash'],'entity_name':company['company_name']},context={'task_id':tid})
    for job in jobs:
        resources.add(job.company,job.source_url,page['retrieved_at'],'web_original_job' if job.evidence_level=='original_detail' else 'web_job_lead',context={'task_id':tid,'title':job.title,'evidence_level':job.evidence_level},
            evidence={'quote':job.description,'content_hash':page['content_hash'],'entity_name':job.company})


def process_task_step(store, tid, browser=None):
    task = store.get(tid)
    if task['state']!='running':
        return
    task=refresh_sites(store,task)
    if 'search_provider_chain' not in task:
        task=store.update(tid,lambda t:bind_providers(store,t))
    if task['usage']['active_seconds']>=task['input']['time_limit_minutes']*60:
        store.update(tid,lambda t:t.update(state='limit_reached') if t['state']=='running' else None)
        store.event(tid,'已到任务时长上限；可调整限制后继续')
        return
    if task.get('recovery_version')!=2:
        task=store.update(tid,migrate_task)
    from .official_queries import extend_official_queries
    if not any(q['channel']=='official_career' and q['status']=='pending' for q in task['queries']):
        task=store.update(tid,extend_official_queries)
    if task.get('execution_policy_version',0)>=2 and any(c['status']=='pending' and not c.get('resource_provenance') and c.get('selection',{}).get('version',0)<SELECTION_VERSION for c in task['candidates']):
        from .discovery import refresh_pending
        task=store.update(tid,refresh_pending)
    all_pending=[c for c in task['candidates'] if c['status'] in ('pending','pending_extract','retry_pending')]
    pending=[c for c in all_pending if ready(c) and candidate_available(task,c)]
    cached=[c for c in pending if c.get('page_snapshot')]
    if (task['usage']['pages']>=task['input']['page_limit'] and not cached) or task['usage']['model_calls']>=task['input']['model_call_limit']:
        store.update(tid,lambda t:t.update(state='limit_reached') if t['state']=='running' else None)
        return
    # Alternate searching and reading; spread detail attempts across city/channel
    # groups so one large first result page cannot monopolize the read budget.
    attempts={}
    for c in task['candidates']:
        if c['status'] not in ('pending','pending_extract','retry_pending','filtered'):
            group=(c['channel'],c['city']);attempts[group]=attempts.get(group,0)+1
    candidate=min(cached or pending,key=lambda c:(not c.get('priority_recovery',False),attempts.get((c['channel'],c['city']),0),c.get('selection',{}).get('priority',3))) if pending else None
    if task.get('execution_policy_version',0)>=2:
        from .scheduling import choose_candidate, execution_progress
        if execution_progress(task)['snippet_reserve_released'] and not task.get('snippet_reserve_released_at'):
            def release(t):
                t['snippet_reserve_released_at']=now()
                t['events'].append({'at':now(),'kind':'info','message':'当前无法继续搜索或读取新页，已读原文整理完毕；剩余模型额度用于已有摘要'})
            task=store.update(tid,release)
        candidate=choose_candidate(task)
    continuing=bool(candidate and candidate.get('page_snapshot') and not candidate.get('evidence_packet'))
    if (not cached or task.get('execution_policy_version',0)>=2) and not continuing and available_query(task) and task['usage']['searches']<=task['usage']['pages']:
        candidate=None
    if candidate:
        reserved = []
        def read_start(t):
            if t['state']!='running':
                return
            c = next(c for c in t['candidates'] if c['id']==candidate['id'])
            if c.get('page_snapshot') or t['usage']['pages']<t['input']['page_limit']:
                c['status']='reading'
                if not c.get('page_snapshot'):
                    t['usage']['pages']+=1
                    c['read_attempts']=c.get('read_attempts',0)+1
                reserved.append(True)
            else:
                t['state']='limit_reached'
        store.update(tid,read_start)
        if not reserved:
            return
        try:
            if candidate.get('page_snapshot'):
                from pathlib import Path
                page = json.loads(Path(candidate['page_snapshot']).read_text(encoding='utf-8'))
            else:
                try:
                    requested=candidate['url']
                    target=requested
                    if urlsplit(requested).scheme=='http':
                        from urllib.parse import urlunsplit
                        parts=urlsplit(requested)
                        target=urlunsplit(('https',parts.netloc.removesuffix(':80'),parts.path,parts.query,parts.fragment))
                        store.update(tid,lambda t:next(c for c in t['candidates'] if c['id']==candidate['id']).update(url_resolution={'state':'https_probe_started','original_url':requested,'attempted_url':target}))
                    access_for(store).check(target)
                    if requires_session(target) and not browser:
                        raise AccessIssue('该招聘网站需要专用 Edge 会话；请启动浏览器后重试','browser_unavailable')
                    if (requires_session(target) or candidate.get('search_route')=='onsite' or candidate.get('preferred_reader')=='browser') and browser:
                        page=browser.read(target)
                    else:page = read_public(target,access=access_for(store))
                except AccessIssue as exc:
                    if classify_issue(exc)['kind']!='dynamic_page' or not browser:
                        raise
                    page = browser.read(target)
                    page['reader_fallback']={'from':'public_https','reason':str(exc),'evidence':exc.evidence}
                if requested!=target:
                    page['url_resolution']={'state':'https_verified','original_url':requested,'attempted_url':target,'final_url':page['url'],'note':'显式探测 HTTPS 成功，不代表观察到 HTTP 重定向'}
                    store.update(tid,lambda t:next(c for c in t['candidates'] if c['id']==candidate['id']).update(url_resolution=page['url_resolution']))
            process_page(store,tid,candidate,page)
        except Exception as exc:
            message = error_text(exc)
            issue=classify_issue(exc)
            site=note_site_issue(store,candidate['url'],exc)
            def failed(t):
                c=next(c for c in t['candidates'] if c['id']==candidate['id'])
                c.setdefault('issue_history',[]).append(issue)
                c.update(status=terminal_status(issue),issue=issue,error=message)
                if site.get('state')!='available':c['access_site_url']=site['url']
                if c.get('url_resolution',{}).get('state')=='https_probe_started':
                    c['url_resolution'].update(state='https_probe_failed',failure_kind=issue['kind'])
                if issue['kind']=='network_transient' and c.get('read_attempts',0)<2:
                    c.update(status='retry_pending',retry_after=retry_time())
                if issue['kind'] in ('rate_limit','site_wait') and site.get('state')=='cooldown':
                    c.update(status='retry_pending',retry_after=retry_timestamp(site['retry_at']))
                elif issue['kind'] in ('rate_limit','site_paused'):
                    c.update(status='blocked')
                t['site_access_state']=access_for(store).snapshot()
                t['events'].append({'at':now(),'message':message+'：'+candidate['title'][:60],'kind':'warning'})
            store.update(tid,failed)
        return
    query = reserve_search(store,tid)
    if query:
        try:
            route=query['search_route']
            if route=='onsite':
                if not browser:raise AccessIssue('专用浏览器不可用','browser_error')
                response=browser.onsite_search(query['channel'],query['city'],query['terms'][0])
            elif route=='browser':
                if not browser:
                    raise AccessIssue('本地浏览器工作区暂不可用，请重新启动后台服务','browser_unavailable')
                response=browser.search(query['query'],query['channel'])
            elif route=='model':
                cfg=store.connection(cid=task['connections']['model'],reveal=True)
                if not cfg:
                    raise AccessIssue('本任务模型连接已移除','configuration_error')
                response=NativeSearch(cfg).search(query['query'],query['channel'])
            else:
                cfg = store.connection(cid=task['connections'].get('search'),reveal=True)
                if not cfg:
                    raise AccessIssue('本任务 Tavily 连接已移除，请配置它或切换为浏览器搜索','configuration_error')
                raw=SearchProvider(cfg).search(query['query'],query['channel'])
                response={k:raw.get(k,{} if k=='usage' else []) for k in ('results','usage')}
                response.update(method='tavily_public_index',proof={'kind':'provider_search_results','provider':'tavily'})
            receipt=search_receipt(store,tid,query,response)
            def success(t):
                q = next(q for q in t['queries'] if q['id']==query['id'])
                finish_attempt(t,q,receipt=receipt)
                q.update(status='done',result_count=len(response.get('results',[])),completed_at=now(),receipt=receipt,method=response['method'])
                add_candidates(t,response.get('results',[]),q)
                t['usage']['search_credits'] += response.get('usage',{}).get('credits',0) or 0
                if route=='model':
                    t['usage']['input_tokens']+=response.get('usage',{}).get('input_tokens',0)
                    t['usage']['output_tokens']+=response.get('usage',{}).get('output_tokens',0)
                if route=='onsite':q['onsite']=response.get('coverage',{})
                if route=='onsite' and not response.get('results'):schedule_index_fallback(t,q)
                t['events'].append({'at':now(),'message':{'onsite':'站内浏览器','browser':'浏览器','model':'模型联网','tavily':'Tavily'}[route]+'完成搜索：'+query['query'],'kind':'info'})
            store.update(tid,success)
            if task.get('quality_version'):
                enqueue_leads(store,tid,query,response,receipt)
        except Exception as exc:
            message = error_text(exc)
            attempted_site=provider_url(query,query['search_route'])
            if attempted_site:note_site_issue(store,attempted_site,exc)
            receipt=search_receipt(store,tid,query,{'error':message,'proof':getattr(exc,'receipt',{})})
            def fail(t):
                t['site_access_state']=access_for(store).snapshot()
                q=next(q for q in t['queries'] if q['id']==query['id'])
                finish_attempt(t,q,failure=classify_failure(exc),message=message,receipt=receipt)
                if getattr(exc,'kind','')=='rate_limit' and attempted_site:
                    site=access_for(store).state(attempted_site)
                    key='onsite:'+q['channel'] if q['search_route']=='onsite' else q['search_route']
                    health=t.get('provider_health',{}).get(key,{})
                    if site.get('retry_at'):health['retry_at']=site['retry_at']
                    if site['state']=='blocked':health['state']='disabled'
                waiting=getattr(exc,'kind','')=='site_wait'
                if not waiting:schedule_index_fallback(t,q)
                t['events'].append({'at':now(),'message':('站点等待中，保留本次已派发记录，不计服务故障或补发额度：' if waiting else '本次搜索失败，已计入次数；继续允许的备用搜索及其他工作：')+message,'kind':'info' if waiting else 'warning'})
            store.update(tid,fail)
        return
    sites=waiting_sites(store.get(tid))
    if any(s['state']=='cooldown' for s in sites):
        return 'site_wait'
    if sites and not pending and not available_query(store.get(tid)):
        def site_blocked(t):
            if t['state']!='running':return
            t.update(state='partial',blocked_reason='剩余页面所在网站等待登录／验证或访问受限；其他可执行工作已完成，原位置和累计预算已保留')
            t['events'].append({'at':now(),'kind':'warning','message':t['blocked_reason']})
        store.update(tid,site_blocked)
        return
    if all_pending:
        cooling=any(not ready(c) and not c.get('evidence_packet') for c in all_pending)
        if task.get('execution_policy_version',0)>=2 and pending and not cooling:
            store.update(tid,lambda t:t.update(state='limit_reached',limit_reason='已到页面或摘要整理分配额度，未处理线索及原文均保留；可调整预算后继续') if t['state']=='running' else None)
        # Retry cooldown is waiting work, not an empty/completed plan.
        return
    def complete(t):
        if t['state']!='running':
            return
        if short_retry_wait(t):return
        reason=drain_reason(t)
        if reason:
            t.update(state='partial',blocked_reason=reason)
            t['events'].append({'at':now(),'message':reason,'kind':'warning'})
            return
        from .login import query_allowed
        outstanding = any(q['status']=='pending' and query_allowed(t,q) for q in t['queries'])
        blocked = any(c['status'] in ('needs_help','system_error','blocked') for c in t['candidates']) or any(q['status'] in ('failed','interrupted') for q in t['queries'])
        t['state'] = 'budget_exhausted' if outstanding else 'partial' if blocked else 'completed'
        t['events'].append({'at':now(),'message':{'budget_exhausted':'搜索预算已用完，结果和待办已保存','partial':'计划已执行，部分查询或页面未完成；详见失败记录','completed':'当前搜索计划已完成'}[t['state']],'kind':'info'})
    store.update(tid,complete)


def command_step(store,browser):
    with store.connect() as db:
        row = db.execute("SELECT * FROM commands WHERE state='pending' ORDER BY rowid LIMIT 1").fetchone()
        if not row:
            return
        db.execute("UPDATE commands SET state='running' WHERE id=?",(row['id'],))
    body, state = json.loads(row['body']), 'done'
    try:
        if row['kind']=='browser_open':
            result = {'tabs':browser.open(body['url'])}
        elif row['kind']=='browser_check_login':
            result=browser.check_login(body['channel'])
        elif row['kind']=='browser_tabs':
            result = {'tabs':browser.tabs()}
        elif row['kind']=='browser_capture':
            result = {'page':browser.capture(body['index'],body.get('expected_url'))}
        elif row['kind']=='browser_close':
            browser.close()
            result = {'tabs':[]}
        elif row['kind']=='browser_search_test':
            response=browser.search(body['query'])
            result={**response,'source_count':len(response['results']),'method':response['method'],
                    'url':response['proof']['url'],'tabs':browser.tabs(),'note':'一次独立浏览器测试，未消耗任务预算'}
        elif row['kind']=='browser_resume_site':
            result=browser.resume_site(body['url'])
            if not result.get('ready'):
                result['message']=result.get('reason') or '页面仍需登录／验证或尚未加载完成，请在专用 Edge 中处理后再检查'
            else:
                access=access_for(store)
                access.release(body['url'])
                if body.get('task_id'):
                    task_id=body['task_id']
                    counts={}
                    def resume(t):
                        counts.update(resume_task_site(t,body['url']))
                        t['site_access_state']=access.snapshot()
                    store.update(task_id,resume)
                    result.update(counts,task_id=task_id)
                result['message']='该网站验证页面已检查；保留已有结果与累计预算'
        else:
            raise ValueError('未知浏览器操作')
    except Exception as exc:
        result, state = {'error':error_text(exc)}, 'failed'
    with store.connect() as db:
        db.execute('UPDATE commands SET state=?, result=? WHERE id=?',(state,json.dumps(result,ensure_ascii=False),row['id']))


def run_worker(folder, stop):
    store = Store(folder)
    store.recover()
    from .login import LoginRegistry
    LoginRegistry(store.path).invalidate('本机服务已重启，请重新检查登录；已有浏览器会话保留')
    browser = Browser(store.path,access=access_for(store))
    yielded_task=None
    try:
        while not stop.is_set():
            command_step(store,browser)
            running=[t for t in store.all() if t['state']=='running']
            ids=[t['id'] for t in running]
            index=(ids.index(yielded_task)+1)%len(ids) if yielded_task in ids else 0
            task=running[index] if running else None
            if task:
                begin = time.monotonic()
                before_wait=access_for(store).total_wait_seconds
                outcome=None
                try:
                    outcome=process_task_step(store,task['id'],browser)
                    yielded_task=task['id'] if outcome=='site_wait' else None
                except Exception as exc:
                    store.update(task['id'],lambda t:t.update(state='paused') if t['state']=='running' else None)
                    store.event(task['id'],error_text(exc),'warning')
                finally:
                    elapsed = 0 if outcome=='site_wait' else max(0,time.monotonic()-begin-(access_for(store).total_wait_seconds-before_wait))
                    store.update(task['id'],lambda t:t['usage'].update(active_seconds=t['usage']['active_seconds']+elapsed))
                stop.wait(.1)
            else:
                stop.wait(.6)
    finally:
        browser.close()
