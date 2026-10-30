"""Execution evidence, separate from search success and vacancy matching."""
from collections import Counter
from urllib.parse import urlsplit
from .models import now
from .traversal import scope_id


def is_content_event(event):
    return event.get('request_purpose', 'robots' if urlsplit(event['url']).path == '/robots.txt' else 'content') != 'robots'


def build_execution_audit(candidates, reports, states, attempts, events, discovery, checkpoint):
    rows = []
    seen = set()
    for candidate in candidates:
        key = scope_id(candidate)
        if key in seen:
            continue
        seen.add(key)
        related = [r for r in reports if r.get('company') == candidate.company and
                   (r.get('url') == candidate.url or r.get('parent_url') == candidate.url)]
        scopes = [s for s in states.values() if s['candidate'].get('company') == candidate.company and
                  (s['candidate']['url'] == candidate.url or any(r.get('url') == s['candidate']['url'] for r in related))]
        called = [a for a in attempts if a['url'] in {r.get('url') for r in related} | {candidate.url}]
        reasons = list(dict.fromkeys([a.get('reason','') for a in called] +
                    [r.get('reason','') for r in related] +
                    [i['reason'] for s in scopes for i in s.get('issues',[])]))
        reasons = [r for r in reasons if r]
        attempted = bool(called) or any(r.get('attempted') for r in related)
        if scopes and all(s['complete'] for s in scopes):
            status = 'complete'
        elif any('navigation_missing' == i['reason'] for s in scopes for i in s.get('issues',[])):
            status = 'no_entry'
        elif attempted or any(s.get('visited') for s in scopes):
            status = 'blocked'
        else:
            status = 'not_executed'
        rows.append(dict(company=candidate.company,url=candidate.url,target_cities=candidate.target_cities,
            scope_id=key,status=status,attempted=attempted,reasons=reasons,
            source_channel=candidate.source_channel,candidate_type=candidate.candidate_type,
            pending=[p for s in scopes for p in s.get('pending',[])],
            list_complete=bool(scopes) and all(s.get('list_complete',False) for s in scopes),
            details_complete=bool(scopes) and all(s.get('details_complete',False) for s in scopes)))
    companies = []
    for name in dict.fromkeys(r['company'] for r in rows if r['company']):
        children = [r for r in rows if r['company'] == name]
        statuses = {r['status'] for r in children}
        status = 'complete' if statuses == {'complete'} else 'blocked' if 'blocked' in statuses else 'not_executed' if 'not_executed' in statuses else 'no_entry'
        companies.append(dict(company=name,status=status,urls=[r['url'] for r in children],
                              reasons=list(dict.fromkeys(x for r in children for x in r['reasons']))))
    continuations = []
    for s in states.values():
        if s['complete']:
            continue
        reasons = [i['reason'] for i in s.get('issues',[])]
        # A timeout is not proof that a browser is required. Keep it retryable in core.
        restricted=any(x in ('blocked','requires_authorization','rate_limited','access_restriction_stop') for x in reasons)
        kind = 'access_restricted_stop' if restricted else 'browser_required' if any('requires_browser' in x for x in reasons) else 'core_pending'
        continuations.append(dict(scope_id=s['scope_id'],kind=kind,candidate=s['candidate'],
            reasons=reasons,pending=s.get('pending',[]),visited=s.get('visited',{}),
            native_checkpoint=checkpoint,python_http_accounts_for_browser=False,
            access_note='逐URL策略及访问拒绝继续生效；此清单不是浏览器已执行或授权凭据'))
    successful = [e for e in events if e.get('status') == 'success' and is_content_event(e)]
    audit = dict(schema_version='core-execution-1',recorded_at=now(),core_invoked=True,
        operation=discovery.get('operation'),plan_id=discovery.get('plan_id'),
        config_fingerprint=discovery.get('config_fingerprint'),input_artifacts=discovery.get('input_artifacts',{}),
        candidate_scopes=len(rows),read_attempts=len(attempts),
        failed_before_http=sum(a.get('failed_before_http',False) for a in attempts),
        http_requests=len(events),successful_content_http_requests=len(successful),
        robots_http_requests=sum(not is_content_event(e) for e in events),
        robots_cache_hits=sum(bool(a.get('robots_check',{}).get('cached')) for a in attempts),
        budget_used=discovery.get('budget_used',{}),
        company_status_counts=dict(Counter(c['status'] for c in companies)),
        native_checkpoint=checkpoint,notes=['调用核心、发出HTTP、取得页面、解析岗位、满足条件分别计量；不包含外部浏览器请求'])
    return audit, dict(schema_version='company-processing-1',companies=companies,candidates=rows), continuations
