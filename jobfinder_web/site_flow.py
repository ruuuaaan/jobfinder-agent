"""Connect shared website access state to task scheduling and targeted recovery."""
import time
from datetime import datetime, timezone
from jobfinder.models import now
from .site_access import SiteAccess, site_key, channel_url


def access_for(store):
    if not hasattr(store, '_site_access'):
        store._site_access = SiteAccess(store.path)
    return store._site_access


def refresh_sites(store, task):
    states = task_sites(access_for(store), task)
    if task.get('site_access_state', {}) != states:
        return store.update(task['id'], lambda t: t.update(site_access_state=states))
    return task


def task_sites(access, task):
    """Project the shared gates onto this task without deleting shared safety state."""
    urls = set()
    for c in task.get('candidates', []):
        urls.update(c.get(k) for k in ('url', 'original_url', 'access_site_url', 'final_url'))
        for evidence in ((c.get('issue') or {}).get('evidence') or {}, c.get('page_snapshot') or {}):
            if isinstance(evidence, dict):
                urls.update(evidence.get(k) for k in ('url', 'final_url'))
    for q in task.get('queries', []):
        providers = [a.get('provider') for a in q.get('attempts', [])]
        providers += [q.get('search_route')]
        if q.get('status') in ('pending', 'running'):
            providers += ['onsite'] if q.get('kind') == 'onsite' else task.get('search_provider_chain', [])
        urls.update(provider_url(q, p) for p in providers)
    keys = {site_key(u) for u in urls if isinstance(u, str) and u.startswith(('https://', 'http://'))}
    states = {k: v for k, v in access.snapshot().items() if k in keys}
    # Older tasks retained login evidence without a shared registry entry.
    for c in task.get('candidates', []):
        issue = c.get('issue') or {}
        if c.get('status') != 'needs_help' or issue.get('kind') not in ('captcha', 'login_required'):
            continue
        url = c.get('access_site_url') or c['url']; key = site_key(url)
        if states.get(key, {}).get('state', 'available') == 'available':
            states[key] = {'site': key, 'state': 'needs_help', 'kind': issue['kind'],
                'reason': issue.get('message') or c.get('error') or '本任务保存的页面仍待登录／验证',
                'retry_at': 0, 'url': url, 'updated_at': 0, 'origin': 'task_evidence'}
    return states


def site_state(task, url):
    value = task.get('site_access_state', {}).get(site_key(url), {})
    if value.get('state') == 'cooldown' and value.get('retry_at', 0) <= time.time():
        return dict(value, state='available')
    return value


def candidate_available(task, candidate):
    return bool(candidate.get('page_snapshot')) or site_state(task, candidate.get('access_site_url') or candidate['url']).get('state', 'available') == 'available'


def provider_url(query, provider):
    if provider == 'onsite':
        return channel_url(query['channel'])
    if provider == 'browser':
        return 'https://cn.bing.com/'
    return ''


def provider_available(task, query, provider, include_cooling=False):
    url = provider_url(query, provider)
    state = site_state(task, url) if url else {}
    return state.get('state', 'available') == 'available' or (include_cooling and state.get('state') == 'cooldown')


def waiting_sites(task):
    """Only gates on outstanding, otherwise budget-eligible work count as waiting."""
    from .search_execution import budget_queries, choices
    rows = []
    for c in task['candidates']:
        if c['status'] in ('pending', 'retry_pending') and not c.get('page_snapshot'):
            s = site_state(task, c.get('access_site_url') or c['url'])
            if s.get('state') in ('cooldown', 'needs_help', 'blocked'):
                rows.append(s)
    for q in budget_queries(task):
        for p in (['onsite'] if q.get('kind') == 'onsite' else task.get('search_provider_chain', [])):
            s = site_state(task, provider_url(q, p))
            if s.get('state') == 'cooldown' and p in choices(task, q, include_cooling=True):
                rows.append(s)
            elif s.get('state') in ('needs_help', 'blocked'):
                rows.append(s)
    return rows


def note_site_issue(store, url, exc):
    access = access_for(store)
    evidence=getattr(exc,'evidence',{}) or {}
    actual=evidence.get('url') or evidence.get('final_url') or url
    if not site_key(actual):actual=url
    if not getattr(exc, '_site_recorded', False):
        access.record_issue(actual, exc)
    return access.state(actual)


def retry_timestamp(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).astimezone().replace(microsecond=0).isoformat()


def resume_task_site(task, url):
    """Rearm only this verified website; retain query IDs, receipts and paid attempts."""
    site = site_key(url)
    candidates = queries = 0
    for c in task['candidates']:
        if site_key(c.get('access_site_url') or c['url']) != site or c['status'] != 'needs_help':
            continue
        if (c.get('issue') or {}).get('kind') not in ('captcha', 'login_required'):
            continue
        c.update(status='pending_extract' if c.get('page_snapshot') else 'pending', priority_recovery=True)
        c.pop('retry_after', None)
        candidates += 1
    for q in task['queries']:
        attempts = q.get('attempts', [])
        last = attempts[-1] if attempts else {}
        provider = last.get('provider', q.get('search_route'))
        if q['status'] not in ('failed', 'pending') or site_key(provider_url(q, provider)) != site:
            continue
        if last.get('failure_type') != 'verification_required':
            continue
        # This is an explicit checked recovery, not an unlimited automatic retry.
        q.update(status='pending', manual_resume_after_attempt=len(attempts), resumed_at=now())
        key = 'onsite:' + q['channel'] if provider == 'onsite' else provider
        task.setdefault('provider_health', {}).pop(key, None)
        queries += 1
    task.setdefault('site_access_state', {}).pop(site, None)
    task.setdefault('site_resumptions', []).append({'site': site, 'at': now(), 'candidates': candidates, 'queries': queries})
    task['events'].append({'at': now(), 'kind': 'info', 'message': f'{site} 验证页面已检查，恢复 {candidates} 个原页面、{queries} 个原查询；累计预算不变'})
    # Verification does not override a user stop, a draft, or an exhausted budget.
    if task['state'] in ('running', 'paused', 'partial'):
        task.update(state='running')
        task.pop('blocked_reason', None)
    return {'resumed_candidates': candidates, 'resumed_queries': queries}
