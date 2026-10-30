"""Resumable, evidence-bound recruitment traversal using the existing request budget."""
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit
from .models import Job, now
from .network import FetchError, request_url, listed_url
from .navigation import job_links, observed_link_allowed, verified_pagination
from .listing_navigation import controls, native_filters
from .parsers import parse_html, ParseError, document, clean
from .matching import deduplicate
from .geography import evaluate_location


def scope_id(candidate):
    return hashlib.sha256(json.dumps([candidate.url,candidate.company,candidate.target_cities],ensure_ascii=False).encode()).hexdigest()[:24]


def initial_state(candidate, reason=None):
    state=dict(schema_version='traversal-1',scope_id=scope_id(candidate),candidate=candidate.model_dump(mode='json'),
        pending=[dict(url=candidate.url,kind='detail' if candidate.candidate_type in ('job_detail','announcement') else 'list',filters=[])],
        visited={},jobs=[],listing_ids=[],terminal=[],issues=[],filter_log=[],skipped_details=[],complete=False,
        list_complete=False,details_complete=False,status='incomplete')
    if reason:
        state.update(entry_pending=True,issues=[{'url':candidate.url,'reason':reason}])
        state['pending'][0]['reason']=reason
    return state


def page_proof(page, url):
    return {k:page.get(k) for k in ('hash','snapshot','retrieved_at')} | {'url':url}


def load_proof(proof):
    path=Path(proof.get('snapshot') or '').resolve()
    from .paths import WORKSPACE as root
    if not path.is_relative_to(root) or not path.is_file():
        raise FetchError('progress_evidence_missing','恢复需要项目内的原页面快照')
    data=path.read_bytes()
    if hashlib.sha256(data).hexdigest()!=proof.get('hash'):
        raise FetchError('progress_evidence_changed','恢复快照哈希不一致')
    return proof | {'data':data}


def job_identity(job):
    return json.dumps([job.company,job.source_id or [job.title,job.locations,job.location_raw,job.description]],ensure_ascii=False)


def annotate_roles(jobs, candidate, page, config):
    if config is None or config.role_mode!='keywords':
        return
    visible=clean(document(page['data']).text_content())
    for job in jobs:
        job.role_review={'decision':'unknown','role_keywords':config.role_keywords}
        for judgment in candidate.role_decisions:
            if (judgment.get('url') in (job.detail_url,job.source_url) and judgment.get('content_hash')==page['hash']
                and judgment.get('role_keywords')==config.role_keywords and judgment.get('quote')
                and clean(judgment['quote']) in visible and judgment.get('rationale')
                and (len(jobs)==1 or job.title in judgment['quote'] or (job.source_id and judgment.get('source_id')==job.source_id))
                and judgment.get('decision') in ('related','out_of_scope')):
                job.role_review=judgment


def skip_detail(job, config):
    if config is None:
        return False
    return (evaluate_location(job,job.location_raw,config.target_cities).state=='FAIL'
            or bool(job.role_review and job.role_review.get('decision')=='out_of_scope'))


def consume(steps):
    """Keep the synchronous API while the service schedules individual read steps."""
    while True:
        try:
            next(steps)
        except StopIteration as done:
            return done.value


def walk(*args, **kwargs):
    return consume(walk_steps(*args, **kwargs))


def walk_steps(fetcher,candidate,first_page,reports,report,config=None,state=None,page_limit=None,targets=None):
    from .moka import bootstrap, attach_city_evidence, read_details
    state=state or initial_state(candidate)
    report['traversal']=state
    if first_page is None:
        report.update(access_status='cached_progress',parse_status='navigation',attempted=False,
                      reason='复用已读范围的原始证据；本次仅处理待办')
    queue=list(state['pending']); blocked=[]
    # Previously failed work can be retried only on this explicit continuation.
    state['issues']=[x for x in state['issues'] if x['reason'] in ('pagination_cycle','duplicate_response','no_loading_progress','terminal_not_proven','requires_browser','adapter_list_incomplete','unparsed_list','detail_requires_browser')]
    cache={request_url(candidate.url):first_page} if first_page else {}
    output=[Job.model_validate(j) for j in state['jobs']]
    seen_ids=set(state['listing_ids'])
    completed=set(state['visited'])
    hashes={p['hash']:key for key,p in state['visited'].items() if p.get('kind')=='list' and p.get('hash')}
    scheduled={node_key(x) for x in queue}
    root_used=False

    def publish():
        state.update(pending=blocked+list(queue),jobs=[j.model_dump(mode='json') for j in deduplicate(output)],
                     listing_ids=sorted(seen_ids),updated_at=now(),complete=False,status='incomplete')
        report.update(coverage_incomplete=True,traversal_status='incomplete')

    def issue(url,reason):
        item={'url':url,'reason':reason}
        if item not in state['issues']: state['issues'].append(item)

    def enqueue(node,parent,page,link=None):
        key=node_key(node)
        if node['kind']=='list' and key in completed:
            issue(node['url'],'pagination_cycle');return
        if key in completed or key in scheduled:
            return
        node['parent']=page_proof(page,parent)
        allowed=observed_link_allowed(fetcher,page,parent,node['url']) if link else listed_url(node['url'],fetcher.policies.get(urlsplit(node['url']).hostname,{}).get('urls',[]))
        status='queued' if allowed else 'policy_pending'
        if allowed and page_limit is not None and len(completed)+len(queue)+1>=page_limit:
            status='budget_exhausted'
        report.setdefault('discovered_links',[]).append({'url':node['url'],'title':(link or {}).get('title','原文详情'),'status':status,'parent_url':parent})
        scheduled.add(key)
        if status=='queued': queue.append(node)
        else:
            blocked.append(node | {'reason':status});issue(node['url'],status)

    started=False
    while queue:
        if started:
            publish()
            yield state
        started=True
        node=queue.pop(0); key=node_key(node); url=node['url']
        if key in completed: continue
        current=candidate.model_copy(update={'url':url,'candidate_type':'job_detail' if node['kind']=='detail' else 'career_entry','pagination_urls':[]})
        r=report if not root_used and url==candidate.url else dict(channel=candidate.source_channel,url=url,parent_url=candidate.url,
            company=candidate.company,target_cities=candidate.target_cities,candidate_type=current.candidate_type,
            access_status='not_run',parse_status='not_run',jobs_extracted=None,reason='')
        root_used=True
        if r is not report: reports.append(r)
        try:
            if node['kind'] not in ('list','detail','moka_detail'):
                raise FetchError('unsupported_progress','进度包含当前不支持的任务类型；保留未完成，需按当前规则重新读取')
            if page_limit is not None and len(completed)>=page_limit:
                raise FetchError('budget_exhausted','显式页面限制已到，未完成遍历')
            if node['kind']=='moka_detail':
                job=next(j for j in output if j.source_id==node['job_id'])
                local=[]
                read_details(fetcher,[job],node['bootstrap'],node['entry'],targets or [],local,r,limit=1)
                reports.extend(local)
                if not job.details_complete:
                    reason=local[-1].get('access_status','requires_browser') if local else 'requires_browser'
                    raise FetchError(reason if reason!='success' else 'requires_browser','岗位接口详情未完成；保留进度')
                state['visited'][key]={'url':url,'kind':'moka_detail','retrieved_at':job.validity_checked_at}
                completed.add(key);continue
            if node.get('parent') and not listed_url(url,fetcher.policies.get(urlsplit(url).hostname,{}).get('urls',[])):
                parent=load_proof(node['parent'])
                if not observed_link_allowed(fetcher,parent,node['parent']['url'],url):
                    raise FetchError('policy_pending','恢复时原导航关系或当前访问策略未通过')
            page=cache.get(request_url(url))
            if page is None:
                page=fetcher.get(url)
            r.update(attempted=True,access_status='success',content_read_at=page['retrieved_at'],validity_checked_at=page['retrieved_at'])
            if node['kind']=='list':
                choices,audits=native_filters(page,url,config,[x['dimension'] for x in node.get('filters',[])])
                state['filter_log'].extend(audits)
                selected_self=next((c for c in choices if request_url(c['url'])==request_url(url)),None)
                choices=[c for c in choices if request_url(c['url'])!=request_url(url)]
                if choices:
                    for choice in choices:
                        enqueue(dict(url=choice['url'],kind='list',filters=[*node.get('filters',[]),choice]),url,page,choice)
                    if not selected_self:
                        state['visited'][key]=page_proof(page,url)|{'kind':'filter_parent'};completed.add(key)
                        r.update(parse_status='navigation',reason='使用页面实际存在且等价的原生筛选链接');continue
                if selected_self:
                    node['filters']=[*node.get('filters',[]),selected_self]
            next_links,pagination,terminal=controls(page,url)
            if node['kind']!='list': next_links,terminal=[],[]
            if node['kind']=='list' and page['hash'] in hashes and hashes[page['hash']]!=key:
                issue(url,'duplicate_response')
                blocked.append(node|{'reason':'duplicate_response'});continue
            if node['kind']=='list': hashes[page['hash']]=key
            try: extracted=parse_html(page['data'],current,page.get('snapshot'),page['retrieved_at'])
            except ParseError: extracted=[]
            embedded=bootstrap(document(page['data']))
            annotate_roles(extracted,candidate,page,config)
            new_ids={job_identity(j) for j in extracted} - seen_ids
            for job in extracted:
                job.validity_checked_at=job.validity_checked_at or page['retrieved_at']
                seen_ids.add(job_identity(job))
            if node['kind']=='detail' and extracted:
                output=[j for j in output if not (j.detail_url==url and any(n.company==j.company for n in extracted))]
            for job in extracted:
                resolved=next((old for old in output if old.company==job.company and old.details_complete and (
                    (job.detail_url and old.source_url==job.detail_url and node_key({'url':job.detail_url,'kind':'detail'}) in completed)
                    or (job.source_id and old.source_id==job.source_id and old.source_channel==job.source_channel
                        and any(e.method=='moka_public_job_detail' for e in old.evidence)))),None)
                if resolved:
                    resolved.evidence.extend(e for e in job.evidence if e not in resolved.evidence)
                else:
                    output.append(job)
            found=job_links(page,url)
            if node['kind']=='list':
                for target in verified_pagination(page['data'],url,candidate.pagination_urls if url==candidate.url else []):
                    if not any(x['url']==target for x in next_links): next_links.append({'url':target,'title':'显式原文分页','kind':'list'})
                    pagination.add(request_url(target))
                # Measure raw loading progress, never the number of matching jobs.
                branch=json.dumps(node.get('filters',[]),sort_keys=True,ensure_ascii=False)
                progress=state.setdefault('loading_progress',{}).setdefault(branch,{'links':[],'stalled':0})
                detail_links={x['url'] for x in found if request_url(x['url']) not in pagination}
                detail_links.update(j.detail_url for j in extracted if j.detail_url and request_url(j.detail_url)!=request_url(url))
                new_links=detail_links-set(progress['links'])
                progress['links']=sorted(set(progress['links']) | detail_links)
                progress['stalled']=0 if new_ids or new_links else progress['stalled']+1
                stalled=bool(next_links) and progress['stalled']>=3
                if stalled:
                    issue(url,'no_loading_progress')
                for link in next_links:
                    if link['url'] is None: issue(url,'requires_browser')
                    elif stalled:
                        blocked.append(dict(url=link['url'],kind='list',filters=node.get('filters',[]),parent=page_proof(page,url),reason='no_loading_progress'))
                    else: enqueue(dict(url=link['url'],kind='list',filters=node.get('filters',[])),url,page,link)
            details={x['url']:x for x in found if request_url(x['url']) not in pagination}
            for job in extracted:
                if job.detail_url and request_url(job.detail_url)!=request_url(url):
                    details.setdefault(job.detail_url,{'url':job.detail_url,'title':job.title,'structured':True})
            for target,link in details.items():
                related=[j for j in extracted if j.detail_url==target or j.source_url==target]
                if related and all(skip_detail(j,config) for j in related):
                    state['skipped_details'].append({'url':target,'reason':'explicit_city_or_evidenced_role_exclusion'});continue
                if node_key({'url':target,'kind':'detail'}) in completed:
                    continue
                for job in related: job.details_complete=False
                enqueue(dict(url=target,kind='detail',filters=node.get('filters',[])),url,page,None if link.get('structured') else link)
            if embedded is not None:
                attach_city_evidence(extracted,document(page['data']),fetcher.policies.get(urlsplit(url).hostname,{}).get('moka_city_code_contract'))
                contract=fetcher.policies.get(urlsplit(url).hostname,{}).get('moka_public_job_read')
                for job in extracted:
                    if job.details_complete or skip_detail(job,config): continue
                    if contract:
                        todo=dict(url=contract['url'],kind='moka_detail',job_id=job.source_id,entry=url,
                                  bootstrap={'org':{'id':embedded['org']['id']},'siteId':embedded.get('siteId')})
                        if node_key(todo) not in scheduled:
                            scheduled.add(node_key(todo));queue.append(todo)
                    else: issue(url,'detail_requires_browser')
                total=(embedded.get('jobStats') or {}).get('total')
                r.update(adapter='moka_html_bootstrap',source_total=total,source_records_read=len(embedded['jobs']))
                count_key=json.dumps(['moka',embedded['org']['id'],node.get('filters',[])],sort_keys=True)
                count_ids=set(state.setdefault('adapter_ids',{}).get(count_key,[])) | {j.source_id for j in extracted if j.source_id}
                state['adapter_ids'][count_key]=sorted(count_ids)
                if type(total) is int and total>=0 and len(count_ids)==total:
                    terminal.append({'kind':'adapter_total','count':total,'source_url':url,'content_hash':page['hash']})
                elif not next_links: issue(url,'adapter_list_incomplete')
            if terminal and not next_links: state['terminal'].extend(terminal)
            if node['kind']=='list' and not next_links and not terminal and embedded is None:
                issue(url,'terminal_not_proven')
            if not extracted and not found and not next_links:
                from .page_reader import dynamic_shell
                dynamic=dynamic_shell(page['data']) and embedded is None
                import re
                empty=bool(re.search(r'暂无(?:招聘)?(?:岗位|职位)|没有符合.*(?:岗位|职位)|no (?:open )?(?:jobs|positions|vacancies)',clean(document(page['data']).text_content()),re.I))
                if terminal and not empty and embedded is None:
                    issue(url,'unparsed_list')
                if dynamic: issue(url,'requires_browser')
                r.update(parse_status='requires_browser' if dynamic else 'listing' if terminal else 'failed',jobs_extracted=0 if terminal and not dynamic else None,reason='已取得末页证据' if terminal else '未取得可解析岗位；不能据此认定无岗位')
                if node['kind']=='detail': blocked.append(node|{'reason':r['parse_status']})
            else:
                r.update(parse_status='success' if extracted else 'listing',jobs_extracted=len(extracted) if extracted else None,reason='已读取原文；列表遍历与岗位条件分别判断')
            state['visited'][key]=page_proof(page,url)|{'kind':node['kind'],'filters':node.get('filters',[])}
            if not any(node_key(x)==key for x in blocked): completed.add(key)
            else: state['visited'].pop(key,None)
        except (FetchError,ParseError,ValueError,OSError) as exc:
            reason=getattr(exc,'status','parse_failed')
            if node['kind']=='moka_detail' and reason in ('blocked','requires_authorization','rate_limited','tls_error') and any(e.get('url')==url for e in getattr(fetcher,'events',[])):
                fetcher.stopped_hosts[urlsplit(url).hostname]=reason
            r.update(access_status=reason,reason=str(exc),jobs_extracted=None)
            issue(url,reason);blocked.append(node|{'reason':reason})
            if reason=='budget_exhausted': blocked.extend(queue);queue=[]
            elif reason in ('rate_limited','blocked','requires_authorization','tls_error'):
                stopped=[x for x in queue if urlsplit(x['url']).hostname==urlsplit(url).hostname]
                blocked.extend(x|{'reason':reason} for x in stopped)
                queue=[x for x in queue if x not in stopped]
    state['pending']=blocked
    state['jobs']=[j.model_dump(mode='json') for j in deduplicate(output)]
    state['listing_ids']=sorted(seen_ids)
    state['updated_at']=now()
    is_detail=candidate.candidate_type in ('job_detail','announcement')
    detail_urls={x['url'] for x in blocked if x['kind'] in ('detail','moka_detail')}
    state['list_complete']=not any(x['kind'] not in ('detail','moka_detail') for x in blocked) and not any(x['url'] not in detail_urls and x['reason']!='detail_requires_browser' for x in state['issues']) and (bool(state['terminal']) or is_detail)
    state['details_complete']=not any(x['kind'] in ('detail','moka_detail') for x in blocked) and not any(not j.details_complete and not skip_detail(j,config) for j in output)
    state['complete']=state['list_complete'] and state['details_complete'] and not state['issues']
    state['status']='completed' if state['complete'] else 'incomplete'
    report.update(coverage_incomplete=not state['complete'],traversal_status=state['status'])
    if report.get('adapter'):
        report['details_incomplete']=sum(not j.details_complete for j in output)
    yield state
    return [Job.model_validate(j) for j in state['jobs']]


def node_key(node):
    return json.dumps([request_url(node['url']),node['kind'],node.get('page_number'),node.get('job_id')],ensure_ascii=False)
