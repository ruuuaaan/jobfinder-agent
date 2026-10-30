"""Query-level execution policy. No network calls and no secret material here."""
import time
from jobfinder.models import now
from jobfinder.spec import effective_spec, ProviderPolicy

PERMANENT={'authentication_error','configuration_error','browser_unavailable','verification_required'}
TRANSIENT={'temporary_network_error','rate_limit','provider_unavailable'}


def attempt_count(query):
    return len(query['attempts']) if 'attempts' in query else int(query['status']!='pending')


def budget_queries(task):
    from .channel_budget import counts, within_budget
    from .login import query_allowed
    used=counts(task)
    return [q for q in sorted(task['queries'],key=lambda q:not bool(q.get('attempts') or q.get('retry_of') or q.get('fallback_of')))
        if q['status']=='pending' and query_allowed(task,q) and within_budget(task,q,used)]


def bind_providers(store,task):
    """Bind only enabled, configured capabilities. Changing connections rearms that provider."""
    from .search_routes import native_profile, NativeSearch
    spec=effective_spec(task)
    mode=spec.search_mode
    allowed=spec.allowed_providers or (['model','browser'] if mode=='auto' else [mode])
    order=([mode]+allowed if mode!='auto' else allowed)
    chain=list(dict.fromkeys(order))
    availability={};costs={};bindings={}
    for provider in chain:
        kind='search' if provider=='tavily' else 'model'
        cfg=store.connection(cid=task['connections'][kind]) if task['connections'].get(kind) else store.connection(kind)
        usable=True;reason=''
        if provider=='model':
            try:usable=bool(cfg and cfg.get('tested') and cfg.get('native_search_tested') and native_profile(cfg)!='none')
            except ValueError:usable=False
            if usable:costs[provider]=NativeSearch(cfg).model_calls
            else:reason='模型联网尚未配置并通过能力测试'
        elif provider=='tavily':
            usable=bool(cfg and cfg.get('tested') and cfg.get('has_key'))
            if not usable:reason='Tavily 尚未配置并通过测试'
        elif provider!='browser':usable=False;reason='未支持的搜索方式'
        availability[provider]={'available':usable,'reason':reason}
        if usable and provider!='browser':
            task['connections'][kind]=cfg['id'];bindings[provider]=cfg['id']
    old=task.get('search_provider_bindings',{})
    for p,binding in bindings.items():
        if p in old and old[p]!=binding:task.setdefault('provider_health',{}).pop(p,None)
    task.update(search_provider_chain=chain,provider_availability=availability,
        search_provider_costs=costs,search_provider_bindings=bindings)
    task['search_route']=next((p for p in chain if availability[p]['available']),chain[0] if chain else mode)


def policy(task):
    return ProviderPolicy.model_validate(task.get('effective_search_spec',{}).get('provider_policy',{}))


def key_for(query,provider):
    return 'onsite:'+query['channel'] if provider=='onsite' else provider


def choices(task,query,*,include_cooling=False):
    candidates=['onsite'] if query.get('kind')=='onsite' else task.get('search_provider_chain',[task.get('search_route') or task['input'].get('search_mode','tavily')])
    p=policy(task);result=[];clock=time.time()
    for provider in candidates:
        if provider=='auto':provider='browser'
        from .site_flow import provider_available
        if not provider_available(task,query,provider,include_cooling):continue
        if not task.get('provider_availability',{}).get(provider,{'available':True})['available']:continue
        health=task.get('provider_health',{}).get(key_for(query,provider),{})
        if health.get('state')=='disabled':continue
        if health.get('state')=='open':
            if health.get('recovery_probes',0)>=p.max_recovery_probes:continue
            if health.get('retry_at',0)>clock and not include_cooling:continue
        attempts=[a for a in query.get('attempts',[])[query.get('manual_resume_after_attempt',0):] if a['provider']==provider]
        if attempts:
            last=attempts[-1]
            if last['status']!='deferred' and (last['status']!='failed' or last.get('failure_type') not in TRANSIENT or len(attempts)>=1+p.transient_retries):continue
            if last.get('retry_at',0)>clock and not include_cooling:continue
        if task['usage']['model_calls']+task.get('search_provider_costs',{}).get(provider,0)>task['input']['model_call_limit']:continue
        result.append((len(attempts)>0,health.get('consecutive_failures',0),provider))
    # Untried healthy alternatives precede retrying a failing service.
    return [x[2] for x in sorted(result,key=lambda x:x[:2])]


def next_query(task):
    return next((q for q in budget_queries(task) if choices(task,q)),None)


def classify_failure(exc):
    kind=getattr(exc,'kind','')
    mapping={'site_wait':'site_wait','network_transient':'temporary_network_error','browser_error':'browser_unavailable',
        'captcha':'verification_required','login_required':'verification_required','access_denied':'verification_required',
        'model_format':'invalid_response','model_connection':'configuration_error','search_parse':'search_result_parse_failure',
        'onsite_city_unconfirmed':'search_result_parse_failure',
        'onsite_submission_unconfirmed':'search_result_parse_failure',
        'onsite_cards_unreadable':'search_result_parse_failure',
        'onsite_controls_missing':'search_result_parse_failure'}
    if kind in PERMANENT|TRANSIENT|{'invalid_response','search_result_parse_failure'}:return kind
    if kind in mapping:return mapping[kind]
    if isinstance(exc,(TimeoutError,ConnectionError,OSError)):return 'temporary_network_error'
    if isinstance(exc,(KeyError,TypeError)):return 'invalid_response'
    # Backward compatibility for untyped browser/provider failures, no response bodies.
    text=str(exc).lower()
    if any(x in text for x in ('验证码','需要验证','请先登录')):return 'verification_required'
    if '429' in text:return 'rate_limit'
    if any(x in text for x in ('401','403','api key')):return 'authentication_error'
    if any(x in text for x in ('timeout','超时','连接失败')):return 'temporary_network_error'
    return 'provider_unavailable'


def begin_attempt(task,query,provider):
    health=task.setdefault('provider_health',{}).setdefault(key_for(query,provider),{})
    if health.get('state')=='open':health['recovery_probes']=health.get('recovery_probes',0)+1
    attempts=query.setdefault('attempts',[])
    attempts.append({'query':query['query'],'channel':query['channel'],'city':query['city'],
        'provider':provider,'attempt_number':len(attempts)+1,'started_at':now(),'finished_at':None,
        'status':'running','failure_type':None,'failure_reason':None,'fallback_provider':None})
    query.update(status='running',search_route=provider,dispatched_at=now())
    task['usage']['searches']+=1
    task['usage']['model_calls']+=task.get('search_provider_costs',{}).get(provider,0)


def finish_attempt(task,query,*,failure=None,message=None,receipt=None):
    attempt=query['attempts'][-1];provider=attempt['provider'];p=policy(task)
    health=task.setdefault('provider_health',{}).setdefault(key_for(query,provider),{})
    if failure=='site_wait':
        attempt.update(status='deferred',finished_at=now(),receipt=receipt,failure_type=None,
                       wait_reason=message,retry_at=time.time()+p.retry_delay_seconds)
        query.update(status='pending',error=None,receipt=receipt)
        return  # Retain the dispatched attempt, but no failure grant or circuit penalty.
    attempt.update(finished_at=now(),receipt=receipt,status='failed' if failure else 'done',failure_type=failure,failure_reason=message)
    if failure:
        from .channel_budget import grant_failure_allowance
        grant_failure_allowance(task,query,attempt)
    if not failure:
        health.update(state='healthy',consecutive_failures=0,last_success_at=now(),retry_at=None)
        query.update(status='done',error=None)
        return
    failures=health.get('consecutive_failures',0)+1
    state='disabled' if failure in PERMANENT else 'open' if failures>=p.failure_threshold or failure=='rate_limit' else 'degraded'
    health.update(state=state,consecutive_failures=failures,last_failure_type=failure,last_failure_reason=message,
        last_failure_at=now(),retry_at=time.time()+p.cooldown_seconds if state=='open' else None)
    attempt['retry_at']=time.time()+p.retry_delay_seconds
    query.update(status='pending',error=message,receipt=receipt)
    fallback=choices(task,query)
    attempt['fallback_provider']=fallback[0] if fallback else None
    if not choices(task,query,include_cooling=True) or query not in budget_queries(task):query['status']='failed'


def search_unavailable(task):
    """Search work remains but no permitted provider can run now."""
    chain=task.get('search_provider_chain')
    all_off=bool(chain) and not any(task.get('provider_availability',{}).get(p,{'available':True})['available']
        and task.get('provider_health',{}).get(p,{}).get('state') not in ('disabled','open') for p in chain)
    return (bool(budget_queries(task)) and next_query(task) is None) or (all_off and not next_query(task))


def short_retry_wait(task):
    for q in budget_queries(task):
        for provider in choices(task,q,include_cooling=True):
            if task.get('provider_health',{}).get(key_for(q,provider),{}).get('state') not in ('open','disabled'):
                return True
    return False


def drain_reason(task):
    if search_unavailable(task):return '没有可用 Search Provider；已完成当前可处理的页面和原文，失败记录、剩余待办及累计预算已保留'
    return None
