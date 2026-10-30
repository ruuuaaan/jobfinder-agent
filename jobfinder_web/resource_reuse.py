"""Reuse recorded career entries, without converting discovery into employer proof."""
import json
from urllib.parse import urlsplit


def successful_details(tasks, inp):
    """Known original pages are read again; never import old jobs as new results."""
    from .quality import role_admission
    from jobfinder.geography import evaluate_location
    from jobfinder.models import Job
    found={}
    for old in tasks:
        rows=old.get('saved_results',[]) if old.get('historical') else [{'job':j} for j in old.get('jobs',[])]
        for row in rows:
            j=row['job']
            if j.get('source_channel')!='official_career' or not j.get('details_complete'):continue
            if j.get('evidence_level') not in (None,'original_detail'):continue
            review=j.get('role_review') or {}
            direct=role_admission(j,inp['terms']+inp.get('adjacent_terms',[]))['eligible']
            duties=role_admission(j,inp['terms'],relation=review.get('decision'),quote=review.get('quote'))['eligible']
            if not (direct or duties):continue
            geo=evaluate_location(Job.model_validate(j),j.get('location_raw'),inp['cities'])
            if geo.state!='PASS':continue
            evidence=[e for e in j.get('evidence',[]) if e.get('content_hash') and e.get('source_url')==j['source_url'] and not any(x in e.get('method','') for x in ('snippet','list'))]
            if not evidence:continue
            ev=evidence[0];url=j['source_url']
            entry={'url':url,'city':geo.city,'observed_at':ev['retrieved_at'],'company':j['company'],
                'title':j['title'],'source_url':url,'basis':'历史完整原岗位；本次重新读取并核验条件和在招状态',
                'preferred_reader':'browser' if 'browser' in ev.get('method','') else 'public',
                'previous_read_method':ev.get('method'),'entry_type':'job_detail','successful_detail':True,
                'historical_task_id':old.get('id'),'original_content_hash':ev['content_hash']}
            if url not in found or entry['observed_at']>found[url]['observed_at']:found[url]=entry
    return list(found.values())


def related_entries(company, inp):
    terms=inp['terms']+inp.get('adjacent_terms',[])
    cities=inp['cities']
    discoveries=company.get('discoveries',[])
    selected=[]; city_context=[]
    for d in discoveries:
        ctx=d.get('context',{});query=ctx.get('query','')
        city_context.extend(city for city in cities if city in ctx.get('target_cities',[]) or city in query)
        if any(t.casefold() in query.casefold() for t in terms):
            related=[city for city in cities if city in ctx.get('target_cities',[]) or city in query]
            if related:selected.append((related[0],d['source_url'],'历史同岗位、同城市查询'))
    # Original hiring-history evidence can establish relevant work, not current hiring.
    for obs in company.get('observations',[]):
        if obs.get('field')!='hiring_history' or obs.get('status')!='source_supported':continue
        try:records=json.loads(obs['value']) if isinstance(obs['value'],str) else obs['value']
        except (ValueError,TypeError):continue
        for job in records if isinstance(records,list) else []:
            related=[city for city in cities if city in job.get('cities',[])]
            if related and any(t.casefold() in job.get('title','').casefold() for t in terms):
                selected.append((related[0],obs['source_url'],'历史原岗位记录；本次重新读取'))
    # Some old directory runs stored role context only in entry evidence.
    finance=any('财务' in t or 'finance' in t.casefold() for t in terms)
    entry_role=finance and any('Finance BP' in json.dumps(d.get('context',{}),ensure_ascii=False) or 'finance-bp' in json.dumps(d.get('context',{}),ensure_ascii=False) for d in discoveries)
    if not selected and not (city_context and entry_role):return []
    city=selected[0][0] if selected else city_context[0]
    entries=[]
    for d in discoveries:
        ctx=d.get('context',{});entry=ctx.get('entry',{})
        if entry.get('entry_type')=='career_entry' and entry.get('source_channel')=='official_career' and entry.get('url'):
            entries.append({'url':entry['url'],'city':city,'observed_at':d['observed_at'],
                'company':company['company_name'],'source_url':d['source_url'],'basis':'历史独立招聘入口，身份及岗位本次复核',
                'preferred_reader':'browser' if 'browser' in entry.get('evidence',{}).get('method','') else 'public',
                'previous_read_method':entry.get('evidence',{}).get('method')})
        for e in ctx.get('career_entries',[]):
            if e.get('url'):entries.append({'url':e['url'],'city':city,'observed_at':d['observed_at'],'company':company['company_name'],
                                          'source_url':d['source_url'],'basis':'历史页面观察到的招聘入口'})
    flat=company.get('career_url') or company.get('official_url')
    if flat:entries.append({'url':flat,'city':city,'observed_at':company['last_seen'],'company':company['company_name'],
                            'source_url':flat,'basis':'企业资源字段'})
    for related,url,basis in selected:
        # Query evidence establishes relevance, not an official recruitment URL.
        # Only reuse it as an entry if a career-entry record/verified field binds
        # its host, or the observed URL is explicitly a career-site path.
        from .domain import source_channel
        host=urlsplit(url).hostname or ''
        bound=any(urlsplit(e['url']).hostname==host for e in entries)
        career=any(word in host for word in ('career.','careers.','recruit.','zhaopin.')) or any(word in urlsplit(url).path.lower() for word in ('/careers','/career/','/joinus'))
        if source_channel(url,'')=='unverified_website' and (bound or career):
            entries.append({'url':url,'city':related,'observed_at':company['last_seen'],'company':company['company_name'],'source_url':url,'basis':basis})
    result=[];seen=set()
    for e in sorted(entries,key=lambda e:(e.get('preferred_reader')=='browser',e['observed_at']),reverse=True):
        if e['url'] not in seen and urlsplit(e['url']).scheme in ('https','http'):
            result.append(e);seen.add(e['url'])
    # Bounded per company, prioritise hiring entry and original related roles.
    return result[:3]
