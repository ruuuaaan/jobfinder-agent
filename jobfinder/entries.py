"""Entry selection and append-only availability evidence; no network or identity search."""
import json
from urllib.parse import urlsplit
from .models import Candidate, now
from .network import request_url
from .resources import name_key


def source_type(candidate):
    if candidate.source_type != 'unknown':
        return candidate.source_type
    if candidate.source_channel == 'public_recruitment':
        return 'repost'
    if candidate.source_channel != 'official_career':
        return 'platform'
    if candidate.official_url and urlsplit(candidate.url).hostname == urlsplit(candidate.official_url).hostname:
        return 'official_website'
    return 'unknown'


def entry_records(company):
    """Project observations without migrating or deleting legacy resource fields."""
    records = {}
    for entry in company.get('entries', []):
        records[(request_url(entry['url']),entry['entry_type'])] = dict(entry)
    for field, kind in [('career_url', 'career_entry'), ('official_url', 'company_home')]:
        if company.get('field_status', {}).get(field) == 'conflict':
            continue
        url = company.get(field)
        if url:
            records.setdefault((request_url(url),kind), dict(url=url, entry_type=kind, availability='unknown',
                source_type='official_website' if kind == 'company_home' else 'unknown',
                source_url=company.get('source_url') or url, checked_at=None,
                reason='legacy_link_requires_current_check'))
    # Legacy job history contains the original page and actual job-city evidence.
    for observation in company.get('observations', []):
        if observation.get('field') != 'hiring_history' or observation.get('status') != 'source_supported':
            continue
        value = observation['value']
        for job in json.loads(value) if isinstance(value, str) else value:
            url = observation['source_url']
            channel = next((d.get('context',{}).get('source_channel') for d in company.get('discoveries',[])
                            if d.get('source_url') == url and d.get('context',{}).get('source_channel')), 'official_career')
            records.setdefault((request_url(url),'job_detail'), dict(url=url, entry_type='job_detail', availability='unknown',
                source_type='unknown', source_channel=channel, source_url=url, checked_at=None, title=job['title'],
                cities=job.get('cities', []), reason='historical_job_requires_current_check'))
    return list(records.values())


def choose_entry(company, knowledge, config, city):
    matches = [c for c in knowledge or [] if name_key(c.get('company_name') or c.get('legal_name') or '') == name_key(company['legal_name'])]
    # Knowledge has current checks; a catalog link must not revive a known dead URL.
    merged = {(request_url(e['url']),e['entry_type']): e for e in entry_records(company)}
    for row in matches:
        merged.update({(request_url(e['url']),e['entry_type']): e for e in entry_records(row)})
    entries = list(merged.values())
    homes = [e for e in entries if e['entry_type'] == 'company_home' and e['availability'] != 'invalid']
    usable = []
    for entry in entries:
        if entry['availability'] in ('invalid', 'job_closed'):
            continue
        if entry.get('source_channel','official_career') not in config.sources:
            continue
        if entry['entry_type'] == 'job_detail':
            if city not in entry.get('cities', []) or (config.role_mode == 'keywords' and not any(k.casefold() in entry.get('title', '').casefold() for k in config.role_keywords)):
                continue
        if entry['entry_type'] in ('job_detail', 'career_entry'):
            usable.append(entry)
    usable.sort(key=lambda e: (e['availability'] not in ('available', 'unknown'), e['entry_type'] != 'job_detail'))
    selected = (usable or homes or [None])[0]
    if selected and selected['entry_type'] == 'job_detail':
        selected = selected | {'parent_career_url':next((e['url'] for e in usable if e['entry_type']=='career_entry'),None)}
    return selected, homes


def entry_task(company, knowledge, config, city, task_id):
    entry, homes = choose_entry(company, knowledge, config, city)
    if entry is None:
        return None
    candidate = Candidate(query='', url=entry['url'], title=entry.get('title') or company['legal_name'],
        company=company['legal_name'], discovered_at=now(), target_cities=[city],
        official_url=homes[0]['url'] if homes else None,
        career_url=entry['url'] if entry['entry_type'] == 'career_entry' else entry.get('parent_career_url'),
        source_channel=entry.get('source_channel','official_career'), source_type=entry.get('source_type', 'unknown'),
        source_evidence=entry.get('evidence',{}) | {'source_url':entry.get('source_url'), 'reason':entry.get('reason'), 'checked_at':entry.get('checked_at')},
        candidate_type=entry['entry_type'], entry_task_id=task_id, discovery_source='knowledge_or_catalog_reuse')
    return dict(task_id=task_id, company=company['legal_name'], target_city=city,
                status='budget_exhausted' if company.get('company_budget_exhausted') else 'planned',
                action='discover_from_home' if entry['entry_type'] == 'company_home' else 'check_entry',
                reason='existing_link_check_before_search', candidate=candidate.model_dump(mode='json'))


def availability(status):
    return {'success':'available', 'not_found':'invalid', 'gone':'invalid',
            'requires_authorization':'restricted', 'blocked':'restricted', 'rate_limited':'restricted',
            'policy_pending':'not_checked', 'robots_disallowed':'restricted',
            'budget_exhausted':'not_checked', 'not_run':'not_checked'}.get(status, 'temporary_failure')


def record_entry(store, candidate, status, reason, page=None, *, entry_type=None, job=None, cities=None):
    if not store or not candidate.company:
        return
    checked = page['retrieved_at'] if page else now()
    entry = dict(url=candidate.url, entry_type=entry_type or candidate.candidate_type,
                 source_channel=candidate.source_channel,
                 source_type=source_type(candidate), source_url=candidate.source_evidence.get('source_url') or candidate.url,
                 checked_at=checked, availability=status, reason=reason,
                 evidence=candidate.source_evidence | ({'content_hash':page['hash'], 'final_url':page.get('final_url',candidate.url)} if page else {}))
    if job:
        entry.update(title=job.title, cities=cities or [])
    store.add(candidate.company, entry['source_url'], checked, 'entry_check', context={'entry':entry})
