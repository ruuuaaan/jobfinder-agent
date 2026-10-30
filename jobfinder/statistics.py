from .planning import cities
from .matching import job_key


def city_statistics(config, jobs, results, reports, discovery):
    output=[]
    for city in cities(config):
        cs=[c for c in discovery.get("candidates",[]) if city in c.get("target_cities",[])]
        rs=[r for r in reports if city in r.get("target_cities",[])]
        qs=[q for q in discovery.get("search_log",[]) if q.get("target_city")==city]
        outcomes=[r for r in results if city in r.matched_cities or r.geo.city==city or r.geo.city is None and city in r.job.discovery_cities]
        output.append(dict(city=city,queries_planned=len(qs),queries_executed=sum(q.get("status") in ("success","failed","restricted") for q in qs),
            companies_discovered=len({c['company'] for c in cs if c.get('company')}),
            companies_checked=len({r['company'] for r in rs if r.get('company') and r.get('attempted')}),
            candidate_links=len({c['url'] for c in cs}),pages_read=sum(r.get('access_status')=='success' for r in rs),
            pages_parsed=sum(r.get('parse_status')=='success' for r in rs),jobs_extracted=0 if discovery.get('operation')=='reexport' else len({job_key(j) for j in jobs if city in j.discovery_cities}),
            jobs_reused=len({job_key(j) for j in jobs if city in j.discovery_cities}) if discovery.get('operation')=='reexport' else 0,
            **{s:len({r.original_job_key for r in outcomes if r.status==s}) for s in ('MATCH','REVIEW','EXCLUDED')},
            restricted=sum(r.get('access_status') in ('blocked','requires_authorization','policy_pending','redirect_pending','official_unverified','rate_limited','navigation_missing') or r.get('parse_status') in ('requires_browser','encoded_payload') for r in rs),
            failed=sum(r.get('access_status') in ('failed','tls_error','robots_unavailable') or r.get('parse_status')=='failed' for r in rs),
            budget_exhausted=sum(r.get('access_status')=='budget_exhausted' for r in rs)+sum(q.get('status')=='budget_exhausted' for q in qs),
            unchecked=sum(r.get('access_status')=='not_run' for r in rs),queries_unchecked=sum(q.get('status') in ('not_run','planned') for q in qs),
            note='提取数按查询归属；结论按实际地点；逐城求和不是全局去重数；未检查不是无岗位'))
    return output
