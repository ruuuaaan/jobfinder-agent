"""Budget-bound entry reuse and home navigation. Search is a separate assistant action."""
from urllib.parse import urlsplit
import json
from .models import now
from .network import FetchError, request_url
from .navigation import discover_career, observed_link_allowed
from .page_reader import extract_page_steps
from .traversal import consume, scope_id, initial_state, page_proof
from .matching import evaluate
from .resources import name_key
from .entries import availability, record_entry, source_type
from .announcements import record_profile


class SharedReader:
    """One run shares both successful reads and terminal failures across cities/paths."""
    def __init__(self, fetcher, memo):
        self.fetcher, self.memo = fetcher, memo

    def __getattr__(self, name):
        return getattr(self.fetcher, name)

    def get(self, url, *args, **kwargs):
        key = request_url(url)
        if key not in self.memo:
            try:
                self.memo[key] = self.fetcher.get(url, *args, **kwargs)
            except FetchError as exc:
                if exc.status in ('budget_exhausted','policy_pending'):
                    raise  # Not attempted: another city or newly observed allowed path may read it.
                self.memo[key] = exc
        value = self.memo[key]
        if isinstance(value, FetchError):
            raise value
        return value

    def _read_api_once(self, method, url, payload):
        key = (method,request_url(url),json.dumps(payload,sort_keys=True))
        if key not in self.memo:
            try:
                self.memo[key] = getattr(self.fetcher,method)(url,payload)
            except FetchError as exc:
                if exc.status in ('budget_exhausted','policy_pending'):
                    raise
                self.memo[key] = exc
        value = self.memo[key]
        if isinstance(value,FetchError):
            raise value
        return value

    def read_moka_job(self, url, payload):
        return self._read_api_once('read_moka_job',url,payload)

def read_entry(fetcher, candidate, config, store, reports, report, resume_states=None):
    return consume(read_entry_steps(fetcher,candidate,config,store,reports,report,resume_states))


def read_entry_steps(fetcher, candidate, config, store, reports, report, resume_states=None, visited_entries=None):
    original = candidate
    queue = [(candidate, 'reuse_or_discovered_link')]
    visited_entries = visited_entries if visited_entries is not None else set()
    parents = {}
    seen, pages, jobs = set(), [], []
    wanted, found_cities = set(candidate.target_cities or config.target_cities[:1]), set()
    last_page = None
    trace = report.setdefault('entry_trace', [])
    report['source_type'] = source_type(candidate)
    report['identity_check'] = 'not_required'
    report['entry_task_id'] = candidate.entry_task_id

    def fallback(reason):
        # Known parent entry is useful when a single vacancy closed/disappeared.
        for url, kind in [(original.career_url, 'career_entry'), (original.official_url, 'company_home')]:
            if url and request_url(url) not in seen and request_url(url) not in visited_entries and not any(request_url(c.url) == request_url(url) for c, _ in queue):
                queue.append((original.model_copy(update={'url':url, 'candidate_type':kind,
                    'source_type':'official_website' if kind == 'company_home' else 'unknown'}), reason))

    def publish_queue():
        pending=[]
        for child,_ in queue:
            state=initial_state(child,'entry_not_started')
            if scope_id(child) in parents:
                state.pop('entry_pending',None)
                state['issues']=[]
                state['pending'][0]['parent']=parents[scope_id(child)]
            pending.append(state)
        report['entry_pending_states']=pending

    started=False
    while queue:
        publish_queue()
        if started:
            yield report
        started=True
        current, reason = queue.pop(0)
        publish_queue()
        key = request_url(current.url)
        if key in seen:
            trace.append(dict(url=current.url, action='skip', status='duplicate', reason='already_checked_in_route'))
            continue
        seen.add(key)
        kind = current.candidate_type
        if kind == 'unknown':
            kind = 'company_home' if current.url == current.official_url else 'career_entry' if current.url == current.career_url else 'job_detail'
            current = current.model_copy(update={'candidate_type':kind})
        attempt = dict(url=current.url, entry_type=kind, action='home_navigation' if kind == 'company_home' else 'read_entry',
                       reason=reason, checked_at=now(), source_type=source_type(current),run_cache_reused=key in fetcher.memo)
        trace.append(attempt)
        step_report = report if key == request_url(original.url) else dict(channel=current.source_channel,url=current.url,
            parent_url=original.url,company=current.company,target_cities=current.target_cities,candidate_type=kind,
            source_type=source_type(current),access_status='not_run',parse_status='not_run',jobs_extracted=None,reason='')
        if step_report is not report:
            reports.append(step_report)
        try:
            report['attempted'] = True
            step_report['attempted'] = True
            saved = (resume_states or {}).get(scope_id(current))
            if saved and saved.get('entry_pending'): saved=None
            page = None if saved else fetcher.get(current.url)
            if page is not None:
                visited_entries.add(key)
                last_page = page
                pages.append((current.url, page))
                step_report.update(access_status='success',content_read_at=page['retrieved_at'],validity_checked_at=page['retrieved_at'])
                attempt.update(status='available', checked_at=page['retrieved_at'])
                record_entry(store, current, 'available', reason, page)
                if source_type(current) in ('official_website', 'recruitment_provider') and current.company:
                    # Extract attributed facts from a page already read; never a prerequisite.
                    record_profile(store, current.company, current.url, page,
                                   current.url if kind == 'company_home' and source_type(current) == 'official_website' else None)
            if kind == 'company_home':
                step_report['resolved_home_scope_id']=scope_id(current)
                step_report.update(parse_status='navigation',reason='已读取首页招聘导航；不是岗位详情')
                choices = discover_career(fetcher, current.model_copy(update={'official_url':current.url}), page)
                report.setdefault('discovered_links', []).extend(x | {'status':'entry_available' if x['approved'] else 'policy_pending', 'parent_url':current.url} for x in choices)
                if not choices:
                    step_report.update(traversal=initial_state(current,'navigation_missing'),coverage_incomplete=True)
                for choice in choices:
                    target_key = request_url(choice['url'])
                    if any(a.get('url') == choice['url'] and a.get('access_status') == 'policy_pending' for a in trace):
                        seen.discard(target_key)  # Newly observed narrow path, not a repeat network request.
                    host, home_host = urlsplit(choice['url']).hostname, urlsplit(current.url).hostname
                    declared_type = fetcher.policies.get(host,{}).get('source_type')
                    root_host = home_host.removeprefix('www.')
                    target_type = declared_type if declared_type in ('official_website','recruitment_provider','platform','repost') else (
                        'official_website' if host == home_host or host.endswith('.'+root_host) else 'unknown')
                    child = current.model_copy(update={'url':choice['url'], 'career_url':choice['url'],
                        'candidate_type':'career_entry', 'source_type':target_type,
                        'source_evidence':{'source_url':current.url, 'content_hash':page['hash'], 'quote':choice['title'], 'relationship':'home_navigation'}})
                    if not choice['approved']:
                        pending=initial_state(child,'policy_pending')
                        pending.pop('entry_pending',None)
                        pending['pending'][0]['parent']=page_proof(page,current.url)
                        reports.append(dict(url=child.url,channel=child.source_channel,company=child.company,
                            target_cities=child.target_cities,access_status='policy_pending',parse_status='not_run',
                            jobs_extracted=None,reason='首页招聘导航待审核',coverage_incomplete=True,traversal=pending))
                        continue
                    queue.append((child, 'observed_home_navigation'))
                    parents[scope_id(child)]=page_proof(page,current.url)
                    if store and current.company and target_type in ('official_website','recruitment_provider'):
                        store.add(current.company,current.url,page['retrieved_at'],'home_navigation',
                            values={'career_url':choice['url']},supported=('career_url',),
                            evidence={'quote':str(choice), 'content_hash':page['hash']})
                attempt['reason'] = 'home_navigation_discovered' if choices else 'navigation_not_found_this_run'
                if any(not c['approved'] for c in choices):
                    attempt['status'] = 'not_checked'
                if current.company_profile_url and observed_link_allowed(fetcher,page,current.url,current.company_profile_url):
                    try:
                        profile = fetcher.get(current.company_profile_url)
                        pages.append((current.company_profile_url,profile))
                        record_profile(store,current.company,current.company_profile_url,profile)
                    except FetchError as exc:
                        trace.append(dict(url=current.company_profile_url,action='profile',status=exc.status,reason=str(exc)))
                continue
            detail_report = step_report
            if kind == 'career_entry':
                report['resolved_career_url'] = current.url
            child_start = len(reports)
            publish_queue()
            extracted = yield from extract_page_steps(fetcher,current,page,reports,detail_report,targets=config.target_cities,config=config,state=saved)
            for child_report in reports[child_start:]:
                if child_report.get('candidate_type') in ('job_detail','career_entry') and child_report.get('access_status') != 'success':
                    child = current.model_copy(update={'url':child_report['url'],'candidate_type':child_report['candidate_type']})
                    state = availability(child_report['access_status'])
                    record_entry(store,child,state,child_report.get('reason','detail_read_failed'))
                    trace.append(dict(url=child.url,entry_type='job_detail',action='read_detail',status=state,reason=child_report.get('reason')))
            for job in extracted:
                job.source_type = source_type(current)
                same_company = not original.company or name_key(original.company) == name_key(job.company)
                if not same_company:
                    job.conflicts['company_attribution'] = [original.company,job.company]
                    job.notes.append('候选企业与原文招聘主体不同；不自动继承集团/子公司关系')
                decisions = evaluate(job, config)
                closed = any(d.dimensions['vacancy'].state == 'FAIL' for d in decisions)
                if same_company and not closed:
                    found_cities.update(d.geo.city for d in decisions if d.geo.state=='PASS' and d.dimensions['role'].state=='PASS' and d.geo.city in wanted)
                # Vacancy checks never invalidate their containing portal.
                job_candidate = current.model_copy(update={'url':job.source_url,'company':job.company,'candidate_type':'job_detail'})
                proof = next((e for e in job.evidence if e.source_url == job.source_url),None)
                proof_page = {'hash':proof.content_hash,'retrieved_at':proof.retrieved_at} if proof else None
                if proof:
                    job_candidate = job_candidate.model_copy(update={'source_evidence':{'source_url':proof.source_url,
                        'content_hash':proof.content_hash,'quote':str(proof.field_quotes)}})
                record_entry(store,job_candidate,'job_closed' if closed else 'available',
                             'original_job_closed' if closed else 'original_job_read',
                             proof_page,entry_type='job_detail',job=job,cities=list(dict.fromkeys(d.geo.city for d in decisions if d.geo.state=='PASS' and d.geo.city)))
                if page is not None and store and same_company and kind == 'career_entry' and job.source_url == current.url and source_type(current) in ('official_website','recruitment_provider'):
                    store.add(job.company,current.url,page['retrieved_at'],'career_original',
                              values={'career_url':current.url},supported=('career_url',),
                              evidence={'quote':job.company+' '+job.title,'content_hash':page['hash']})
            jobs.extend(extracted)
            report.update(matched_cities=sorted(found_cities),fallback_target_cities=sorted(wanted-found_cities))
            attempt['parse_status'] = detail_report.get('parse_status')
            if wanted <= found_cities:
                report.update(entry_jobs_extracted=len(jobs),entry_outcome='relevant_job_found',fallback_search_reason=None)
                queue=[(c,why) for c,why in queue if why=='observed_home_navigation']
                publish_queue()
                if not queue:
                    return current,last_page,pages,jobs
                continue
            attempt['reason'] = 'no_matching_job_this_run' if extracted else 'no_parsed_job_this_run'
            fallback(attempt['reason'])
        except FetchError as exc:
            state = availability(exc.status)
            attempt.update(status=state,access_status=exc.status,reason=str(exc))
            record_entry(store,current,state,str(exc))
            step_report.update(access_status=exc.status,reason=str(exc))
            if not step_report.get('traversal'):
                step_report['traversal']=initial_state(current,exc.status)
                step_report['coverage_incomplete']=True
            if exc.status == 'budget_exhausted':
                report.update(entry_outcome='budget_exhausted',fallback_search_reason='request_budget_exhausted',fallback_target_cities=sorted(wanted-found_cities))
                trace.extend(dict(url=c.url,action='not_run',status='budget_exhausted',reason='request_budget_exhausted') for c,_ in queue)
                for queued,_ in queue:
                    reports.append(dict(url=queued.url,channel=queued.source_channel,company=queued.company,
                        target_cities=queued.target_cities,access_status='not_run',parse_status='not_run',jobs_extracted=None,
                        reason='request_budget_exhausted',coverage_incomplete=True,traversal=initial_state(queued,'budget_exhausted')))
                publish_queue()
                return current,last_page,pages,jobs
            if exc.status in ('blocked','requires_authorization','rate_limited'):
                # A denial is not an invitation to try a parent or alternate portal.
                for queued,_ in queue:
                    pending=initial_state(queued,'access_restriction_stop')
                    reports.append(dict(url=queued.url,channel=queued.source_channel,company=queued.company,
                        target_cities=queued.target_cities,access_status='not_run',parse_status='not_run',
                        jobs_extracted=None,reason='access_restriction_stop',coverage_incomplete=True,traversal=pending))
                queue=[]
                publish_queue()
                report.update(entry_outcome='incomplete',fallback_search_reason=None,
                              reason=str(exc),entry_jobs_extracted=len(jobs))
                return current,last_page,pages,jobs
            fallback('invalid_entry' if state == 'invalid' else 'restricted_or_temporary_entry_failure')
    publish_queue()
    incomplete = any(a.get('status') in ('restricted','temporary_failure','not_checked') or a.get('parse_status') in ('requires_browser','failed') for a in trace)
    report.update(entry_outcome='incomplete' if incomplete else 'not_found_this_run',
                  fallback_target_cities=sorted(wanted-found_cities),
                  fallback_search_reason='entry_checks_incomplete' if incomplete else 'no_matching_job_this_run',
                  reason='本次入口检查未完成，不能判断无岗位' if incomplete else '本次未找到匹配岗位；不表示企业没有岗位',
                  entry_jobs_extracted=len(jobs))
    return original,last_page,pages,jobs
