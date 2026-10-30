from __future__ import annotations
import io
import json
import secrets
import uuid
import copy
from functools import lru_cache
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Literal
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ConfigDict, ValidationError
from openpyxl import Workbook
from jobfinder.models import Job, SearchConfig, now
from jobfinder.matching import evaluate, deduplicate, job_key
from jobfinder.exporter import sheet, SOURCE_LABELS, SUMMARY_HEADERS, summary_review_note
from jobfinder.resources import ResourceStore
from .storage import Store, ROOT, DATA
from .domain import CHANNELS, TaskInput, new_task, role_suggestions, make_queries, query_coverage
from .providers import ModelProvider, SearchProvider, validate_url, AccessIssue
from .worker import add_candidates, save_page, seed_resource_entries
from .rules import snapshot_rules, model_rules
from .search_routes import MODES, NATIVE_PROFILES, NativeSearch, native_profile, resolve_mode
from .recovery import migrate_task, HUMAN, BLOCKED
from .batches import progress as batch_progress
from jobfinder.spec import effective_spec, task_contract, revise_execution
from jobfinder.education import LABELS as EDUCATION_LABELS, RANK, RULES_VERSION
from .search_execution import bind_providers, attempt_count
from .site_flow import access_for, task_sites
from .site_access import channel_url, site_key
from .login import LoginRegistry, PLATFORMS, prepare_plan
from .history import archive, imported_task, MAX_BYTES
from jobfinder import __version__

LABELS = SOURCE_LABELS | {k:v['name'] for k,v in CHANNELS.items()} | {'unverified_website':'公开网页（身份待核验）',
    'public_recruitment':'公共招聘平台','hr_official':'人社官方公告','university_career':'高校就业网','third_party_repost':'第三方转载'}


class ConnectionInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(default='自定义兼容接口',max_length=80)
    base_url: str = Field(default='',max_length=500)
    model: str = Field(default='',max_length=150)
    api_key: str | None = Field(default=None,max_length=6000)
    allow_local: bool = False
    native_search: Literal['auto','none','deepseek','dashscope','responses'] = 'auto'


class SearchModeInput(BaseModel):
    mode: Literal['auto','model','browser','tavily']
    allowed_providers: list[Literal['model','browser','tavily']] | None = None


@lru_cache(maxsize=4096)
def evaluated_record(job_json,config_json,day):
    # Polling progress must not repeat expensive geography/education evaluation
    # for every unchanged record. Include local day for deadline rollover.
    job=Job.model_validate_json(job_json);config=parsed_matching_config(config_json)
    return [r.model_dump(mode='json') for r in evaluate(job,config)]


@lru_cache(maxsize=128)
def parsed_matching_config(config_json):
    return SearchConfig.model_validate_json(config_json)


def task_results(task):
    if task.get('historical'):
        return task.get('saved_results',[])
    config = effective_spec(task).to_config()
    day=datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
    return [copy.deepcopy(r) for j in deduplicate([Job.model_validate(j) for j in task['jobs']])
            for r in evaluated_record(j.model_dump_json(),config.model_dump_json(),day)]


def summary(task):
    task=task_contract(task)
    from .scheduling import execution_progress
    from .channel_budget import summary as search_budget_summary, channel_progress
    results = task_results(task)
    visible_jobs={r.get('original_job_key') or job_key(Job.model_validate(r['job'])):r['job'] for r in results if r['status']!='EXCLUDED'}
    counts = {s:len({r.get('original_job_key') or job_key(Job.model_validate(r['job'])) for r in results if r['status']==s}) for s in ('MATCH','REVIEW','EXCLUDED')}
    outcomes={status:sum(c['status']==status for c in task['candidates']) for status in ('needs_help','system_error','blocked','filtered','retry_pending','pending_extract')}
    return {k:v for k,v in task.items() if k not in ('jobs','saved_results')} | {'counts':counts,'job_count':sum(counts.values()),'outcomes':outcomes,
        'query_coverage':query_coverage(task),'search_budget_summary':search_budget_summary(task),
        'channel_search_progress':channel_progress(task),
        'execution_progress':execution_progress(task),
        'evidence_counts':{key:sum(j.get('evidence_level')==key for j in visible_jobs.values()) for key in ('search_snippet','onsite_list','original_detail')},
        'complete_count':sum(bool(j.get('details_complete')) for j in visible_jobs.values()),
        'eligible_count':sum(c['status'] not in ('filtered','superseded') and not c.get('evidence_packet') for c in task['candidates']),
        'batch_progress':{'done':sum(batch_progress(c)['done'] for c in task['candidates']),'total':sum(batch_progress(c)['total'] for c in task['candidates'])},
        'visible_count':counts['MATCH']+counts['REVIEW'],'candidate_count':len(task['candidates']),
        'read_count':sum(c['status']=='read' and not c.get('evidence_packet') for c in task['candidates'])}


def create_app(folder=DATA):
    store = Store(folder)
    app = FastAPI(title='JobFinder 本机工作台', docs_url=None, redoc_url=None)
    app.state.store = store
    token = secrets.token_urlsafe(32)

    @app.middleware('http')
    async def local_only(request, call_next):
        host = request.url.hostname
        if host not in ('127.0.0.1','localhost','::1','testserver'):
            return JSONResponse({'detail':'仅允许本机访问'},status_code=403)
        if request.method not in ('GET','HEAD','OPTIONS'):
            origin = request.headers.get('origin')
            if origin and urlsplit(origin).netloc != request.headers.get('host'):
                return JSONResponse({'detail':'请求来源不匹配'},status_code=403)
            if request.headers.get('x-jobfinder-token') != token:
                return JSONResponse({'detail':'请刷新本机页面后重试'},status_code=403)
        response = await call_next(request)
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='no-referrer'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; object-src 'none'"
        if request.url.path.startswith('/api'):
            response.headers['Cache-Control']='no-store'
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({'detail':'输入格式有误，请检查字段和数值范围','fields':[list(e['loc']) for e in exc.errors()]},status_code=422)

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        message = '输入未通过校验，请检查城市、学历、预算和必填项' if isinstance(exc,ValidationError) else str(exc)[:400]
        return JSONResponse({'detail':message},status_code=400)

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({'detail':'记录不存在'},status_code=404)

    @app.get('/api/health')
    def health():
        return {'ok':True,'service':'JobFinder','version':__version__,'local_only':True}

    @app.get('/api/bootstrap')
    def bootstrap():
        return {'token':token,'version':__version__,'channels':CHANNELS,'source_labels':LABELS,'connections':{k:store.connection(k) for k in ('model','search')},'history_available':True,
                'search_modes':MODES,'native_profiles':NATIVE_PROFILES,'rules_version':RULES_VERSION,
                'candidate_education_options':{k.value:EDUCATION_LABELS[k] for k in RANK},
                'task_defaults':{k:TaskInput.model_fields[k].default for k in ('page_limit','model_call_limit')}}

    @app.get('/api/connections')
    def connections():
        return {k:store.connection(k) for k in ('model','search')}

    @app.get('/api/site-access')
    def site_access(task_id:str|None=None):
        access=access_for(store)
        if task_id:
            states=task_sites(access,store.get(task_id))
        else:
            # Settings only shows the four pre-login sites, never the historical registry.
            states={site_key(channel_url(c)):access.state(channel_url(c)) for c in ('boss','zhaopin','liepin','51job')}
        return {'sites':list(states.values()),'scope':'task' if task_id else 'prelogin','policy':{
            'min_interval_seconds':access.policy.min_interval,
            'rate_limit_cooldown_seconds':access.policy.rate_limit_cooldown}}

    @app.get('/api/login-status')
    def login_status():
        states=LoginRegistry(store.path).states();access=access_for(store)
        for c,s in states.items():
            gate=access.state(channel_url(c))
            s.update(access_state=gate['state'],access_reason=gate.get('reason',''))
        return {'platforms':states}

    @app.put('/api/tasks/{tid}/login-plan')
    def login_plan(tid:str,body:dict):
        if set(body)!={'routes'} or not isinstance(body['routes'],dict):raise ValueError('账号准备参数无效')
        states=LoginRegistry(store.path).states()
        def change(t):
            if t.get('historical') or t['state']=='running':raise ValueError('请先暂停任务再调整账号准备')
            prepare_plan(t,body['routes'],states)
        return summary(store.update(tid,change))

    @app.put('/api/connections/{kind}')
    def save_connection(kind:str, body:ConnectionInput):
        if kind not in ('model','search'):
            raise ValueError('连接类型无效')
        old = store.connection(kind)
        if kind=='model':
            if not body.base_url.strip() or not body.model.strip():
                raise ValueError('请填写接口地址和模型名称')
            validate_url(body.base_url,body.allow_local)
            if urlsplit(body.base_url).query or urlsplit(body.base_url).fragment:
                raise ValueError('接口地址不能包含查询参数或片段；API Key 请只填入专门的密钥栏')
            native_profile(body.model_dump(exclude={'api_key'}))
            if old and body.base_url.rstrip('/')!=old.get('base_url','').rstrip('/') and not body.api_key:
                raise ValueError('接口地址已变化，请重新填写该服务的密钥')
        if not body.api_key and not (old and old['has_key']) and not (kind=='model' and body.allow_local):
            raise ValueError('请在本机设置页填写 API Key')
        data = body.model_dump(exclude={'api_key'}) | {'tested':False,'native_search_tested':False,'saved_at':now()}
        return store.save_connection(kind,data,body.api_key or None)

    @app.post('/api/connections/model/web-search/test')
    def test_native_search():
        cfg = store.connection('model',reveal=True)
        if not cfg or not cfg.get('tested'):
            raise ValueError('请先保存并测试模型连接，再测试联网能力；浏览器模式无需此项测试')
        try:
            result = NativeSearch(cfg).search('广州 财务BP 招聘')
            capability = {'profile':native_profile(cfg),'source_count':len(result['results']),
                          'method':result['method'],'tested_at':now(),'tested':bool(result['results'])}
            if not capability['tested']:
                raise AccessIssue('搜索未返回来源，暂不认定联网测试通过；可使用浏览器模式')
        except Exception:
            with store.connect() as db:
                body = {k:v for k,v in cfg.items() if k not in ('api_key','has_key','kind','id')}
                body['native_search_tested']=False
                db.execute('UPDATE connections SET body=? WHERE id=?',(json.dumps(body,ensure_ascii=False),cfg['id']))
            raise
        with store.connect() as db:
            body = {k:v for k,v in cfg.items() if k not in ('api_key','has_key','kind','id')}
            body.update(native_search_tested=True,native_capabilities=capability)
            db.execute('UPDATE connections SET body=? WHERE id=?',(json.dumps(body,ensure_ascii=False),cfg['id']))
        return {'ok':True,'result':capability,'note':'已发出一次模型服务联网请求，独立于任务预算，可能按服务商规则计费'}

    @app.post('/api/connections/{kind}/test')
    def test_connection(kind:str):
        cfg = store.connection(kind,reveal=True)
        if not cfg:
            raise ValueError('请先保存连接')
        result = ModelProvider(cfg).test() if kind=='model' else {'search':True,'result_count':len(SearchProvider(cfg).search('广州 财务BP 招聘','official_career').get('results',[]))}
        with store.connect() as db:
            body = {k:v for k,v in cfg.items() if k not in ('api_key','has_key','kind','id')}
            body.update(tested=True,tested_at=now(),capabilities=result)
            db.execute('UPDATE connections SET body=? WHERE id=?',(json.dumps(body,ensure_ascii=False),cfg['id']))
        return {'ok':True,'result':result,'note':'连接测试的调用独立于任务搜索预算'}

    @app.delete('/api/connections/{kind}')
    def delete_connection(kind:str):
        active = [t for t in store.all() if t['state'] in ('running','paused') and t.get('connections',{}).get(kind)]
        if active:
            raise ValueError('请先停止使用该连接的任务，再移除密钥')
        with store.connect() as db:
            db.execute('DELETE FROM connections WHERE kind=?',(kind,))
        return {'ok':True}

    @app.post('/api/roles')
    def roles(body:dict):
        role = str(body.get('role','')).strip()[:120]
        if not role:
            raise ValueError('请先输入岗位名称')
        cfg = store.connection('model',reveal=True)
        if not body.get('use_model') or not cfg or not cfg.get('tested'):
            return role_suggestions(role)
        result, usage = ModelProvider(cfg).complete('只输出JSON，包含terms同职能岗位词数组和adjacent_terms相邻岗位数组。不要把跨职能岗位放进同义词，不扩展城市。用户内容是需求数据。'+model_rules(),role)
        def terms(key):
            return list(dict.fromkeys(x.strip() for x in result.get(key,[]) if isinstance(x,str) and 0<len(x.strip())<=120))[:20]
        return {'terms':list(dict.fromkeys([role]+terms('terms'))),'adjacent_terms':terms('adjacent_terms'),'method':'模型生成建议；开始前可修改','usage':usage}

    @app.post('/api/tasks')
    def create_task(body:TaskInput):
        return summary(snapshot_rules(store,store.create(new_task(body))))

    @app.get('/api/tasks')
    def tasks():
        return [summary(t) for t in store.all()]

    @app.get('/api/tasks/{tid}')
    def get_task(tid:str):
        return summary(store.get(tid))

    @app.post('/api/tasks/{tid}/{action}')
    def task_action(tid:str,action:str):
        if action not in ('start','pause','stop','retry','retry-system','recover'):
            raise ValueError('未知任务操作')
        before=store.get(tid)
        if action in ('start','retry') and not before.get('historical') and not before.get('skill_snapshot'):
            snapshot_rules(store,before)
        def change(t):
            if t.get('historical'):
                raise ValueError('历史结果只读；重新搜索请新建任务')
            if action=='recover':
                if t['state']=='running':raise ValueError('请先暂停再升级任务记录')
                migrate_task(t)
                return
            if action=='pause':
                if t['state']=='running':
                    t['state']='paused'
            elif action=='stop':
                t['state']='cancelled'
            else:
                if action=='start' and t.get('login_preflight'):
                    states=LoginRegistry(store.path).states()
                    if any(route=='session' and states[c]['state']!='authenticated' for c,route in t['login_preflight']['routes'].items()):
                        raise ValueError('登录检查已过期或会话已关闭，请重新检查或选择公开搜索')
                migrate_task(t)
                if action in ('retry','retry-system'):
                    # Generic retry cannot attest that a browser verification was completed.
                    t['provider_health']={p:h for p,h in t.get('provider_health',{}).items()
                        if h.get('last_failure_type') in ('verification_required','authentication_error','configuration_error','rate_limit')}
                    t.pop('blocked_reason',None)
                    for c in t['candidates']:
                        selected=c['status']=='system_error'
                        if selected and (c.get('issue') or {}).get('kind') not in BLOCKED:
                            c['status']='pending_extract' if c.get('page_snapshot') else 'pending'
                            c.pop('retry_after',None)
                            for b in c.get('batches',[]):
                                if b['status']=='failed':b['status']='pending'
                    def retryable(q):
                        last=(q.get('attempts') or [{}])[-1]
                        return last.get('failure_type') not in ('verification_required','authentication_error','configuration_error','rate_limit') and q.get('site_issue_kind') not in HUMAN|BLOCKED
                    retries = [{k:q[k] for k in ('city','channel','query','terms','kind') if k in q} | {'id':uuid.uuid4().hex[:20], 'status':'pending','retry_of':q['id']} for q in t['queries'] if q['status'] in ('failed','interrupted') and retryable(q) and not any(p.get('retry_of')==q['id'] for p in t['queries'])]
                    t['queries'].extend(retries)
                model_cfg = store.connection(cid=t['connections']['model']) if t['connections'].get('model') else store.connection('model')
                if not model_cfg or not model_cfg.get('tested'):
                    raise ValueError('请先在连接设置中保存并测试模型 API；浏览器搜索无需 Tavily')
                t['connections']['model']=model_cfg['id']
                bind_providers(store,t)
                seed_resource_entries(store,t)
                if not any(x['available'] for x in t['provider_availability'].values()) and not any(q.get('kind')=='onsite' and q['status']=='pending' for q in t['queries']) and not any(c['status'] in ('pending','pending_extract','retry_pending') for c in t['candidates']):
                    t.update(state='partial',blocked_reason='没有可用 Search Provider；请检查已启用服务的配置与测试结果，累计预算和待办已保留')
                    t['events'].append({'at':now(),'kind':'warning','message':t['blocked_reason']})
                    return
                t['state']='running'
            t['events'].append({'at':now(),'message':{'start':'任务已开始／继续，沿用累计预算','pause':'已暂停；当前已发出的请求会完成并保存','stop':'已停止；当前请求返回后保留已取得结果','retry':'已安排可恢复系统异常；登录或验证请使用对应网站的检查并继续','retry-system':'已安排系统异常重试；访问限制不重试，已用次数不退回'}[action],'kind':'info'})
        return summary(store.update(tid,change))

    @app.put('/api/tasks/{tid}/search-mode')
    def update_search_mode(tid:str, body:SearchModeInput):
        def change(t):
            if t.get('historical') or t['state']=='running':
                raise ValueError('请先暂停任务再更换搜索方式；历史结果不可更改')
            t['input']['search_mode']=body.mode
            allowed=body.allowed_providers or (['model','browser'] if body.mode=='auto' else [body.mode])
            if body.mode!='auto' and body.mode not in allowed:raise ValueError('须启用所选搜索服务')
            t['input']['allowed_providers']=allowed
            revise_execution(t,{'search_mode':body.mode,'allowed_providers':allowed})
            t.pop('search_route',None)
            t.pop('search_provider_chain',None)
            t.pop('provider_health',None)
            t.pop('blocked_reason',None)
            t['connections'].pop('search',None)
            # Rebind the newly tested model on the next start, never clear usage.
            t['connections'].pop('model',None)
            t['events'].append({'at':now(),'message':'搜索方式改为 '+MODES[body.mode]+'；已用预算和旧来源保持不变','kind':'info'})
        return summary(store.update(tid,change))

    @app.put('/api/tasks/{tid}/budget')
    def budget(tid:str,body:dict):
        allowed = {'city_budgets','channel_budgets','page_limit','model_call_limit','time_limit_minutes'}
        if set(body)-allowed:
            raise ValueError('此处仅可调整预算限制')
        def change(t):
            if t.get('historical'):
                raise ValueError('历史结果不使用搜索预算')
            inp = TaskInput.model_validate({'search_mode':'tavily'} | t['input'] | body)
            from .channel_budget import PRIORITY, limits, counts
            if inp.budget_policy==PRIORITY:
                proposed=dict(t,input=inp.model_dump())
                city_limits,pair_limits=limits(proposed)
                city_used,_,pair_used=counts(t)
                if any(city_used[c]>n for c,n in city_limits.items()) or any(pair_used[p]>n for p,n in pair_limits.items()):
                    raise ValueError('分配后的城市或渠道额度不能小于已消耗次数；一次性补充仍保留')
            else:
                for city,n in inp.city_budgets.items():
                    if n<sum(attempt_count(q) for q in t['queries'] if q['city']==city):
                        raise ValueError('城市预算不能小于已消耗次数（含执行中）')
                for channel,n in inp.channel_budgets.items():
                    if n<sum(attempt_count(q) for q in t['queries'] if q['channel']==channel):
                        raise ValueError('渠道预算不能小于已消耗次数')
            if inp.page_limit<t['usage']['pages'] or inp.model_call_limit<t['usage']['model_calls'] or inp.time_limit_minutes*60<t['usage']['active_seconds']:
                raise ValueError('限制不能低于已消耗用量')
            t['input']=inp.model_dump()
            t['budget_revisions'].append({'at':now(),'cities':inp.city_budgets,'channels':inp.channel_budgets,'budget_policy':inp.budget_policy})
            t['events'].append({'at':now(),'message':'已更新预算，历史用量保持不变','kind':'info'})
        return summary(store.update(tid,change))

    @app.post('/api/tasks/{tid}/links/add')
    def add_link(tid:str,body:dict):
        url = str(body.get('url',''))
        validate_url(url)
        def change(t):
            if t.get('historical'):
                raise ValueError('请将链接加入新搜索任务')
            add_candidates(t,[{'url':url,'title':'手动补充原文链接'}],{'id':'manual_link','channel':body.get('channel','official_career'),'city':t['input']['cities'][0]})
        return summary(store.update(tid,change))

    @app.get('/api/results')
    def results(task_id:str|None=None):
        selected = [store.get(task_id)] if task_id else store.all()
        records=[]
        seen=set()
        for task in selected:
            for r in task_results(task):
                key = (r['job']['source_url'],r['job']['company'],r['job']['title'],r['job'].get('location_raw'))
                if not task_id and key in seen:
                    continue
                seen.add(key)
                records.append(r | {'task_id':task['id'],'historical':task.get('historical',False),'source_label':LABELS.get(r['job']['source_channel'],r['job']['source_channel'])})
        return records

    @app.get('/api/export')
    def export(task_id:str|None=None):
        records = results(task_id)
        book=Workbook();book.remove(book.active)
        rows=[];seen=set()
        for r in records:
            if r['status'] not in ('MATCH','REVIEW') or r.get('geo',{}).get('state')=='FAIL':
                continue
            j=r['job']; key=(j['source_url'],j['company'],j['title'],j.get('location_raw'))
            if key in seen:
                continue
            seen.add(key)
            note=summary_review_note(r['status'],r.get('pending'),r.get('historical',False))
            rows.append([j['company'],j['title'],j['description'],j.get('salary_raw') or '未披露',j.get('application_url') or j.get('detail_url') or j['source_url'],j.get('location_raw') or '未披露',j.get('education_raw') or '未披露',j.get('experience_raw') or '未披露',r['source_label'],note])
        sheet(book,'岗位汇总',SUMMARY_HEADERS,rows)
        buffer=io.BytesIO();book.save(buffer)
        return Response(buffer.getvalue(),media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers={'Content-Disposition':'attachment; filename="JobFinder-jobs.xlsx"'})

    @app.get('/api/tasks/{tid}/evidence')
    def evidence(tid:str):
        return store.get(tid) | {'results':task_results(store.get(tid))}

    @app.post('/api/history/import')
    async def import_history(request: Request):
        chunks = bytearray()
        async for chunk in request.stream():
            chunks.extend(chunk)
            if len(chunks) > MAX_BYTES:
                raise HTTPException(status_code=413, detail='结果文件不能超过 10 MiB')
        try:
            data = json.loads(chunks.decode('utf-8-sig'))
        except (UnicodeError, json.JSONDecodeError):
            raise ValueError('结果文件不是有效的 UTF-8 JSON')
        task = imported_task(data)
        for t in store.all():
            if t.get('import_sha256')==task['import_sha256']:
                return summary(t)
        return summary(store.create(task))

    @app.get('/api/history/export')
    def export_history(task_id: str):
        task = task_contract(store.get(task_id))
        content = json.dumps(archive(task, task_results(task)), ensure_ascii=False, indent=2).encode('utf-8')
        return Response(content, media_type='application/json', headers={'Content-Disposition':'attachment; filename="jobfinder-results.json"'})

    @app.post('/api/results/merge')
    def merge_results(body:dict):
        ids=list(dict.fromkeys(body.get('task_ids',[])))
        if len(ids)<2:
            raise ValueError('请至少选择两个任务')
        selected=[store.get(i) for i in ids]
        records=[];seen=set()
        for t in selected:
            for r in task_results(t):
                j=r['job']; key=(j['source_url'],j['company'],j['title'],j.get('location_raw'))
                if key not in seen:
                    seen.add(key);records.append(r)
        # CLI archives may use agent_assisted and channels not offered by the web form.
        base=imported_task(archive(task_contract(selected[0]),records))
        base.pop('import_sha256',None)
        base.update(state='historical',historical=True,queries=[],saved_results=records,jobs=[r['job'] for r in records],merged_from=ids)
        base['input']['role']='合并结果 · '+base['input']['role']
        return summary(store.create(base))

    @app.get('/api/companies')
    def companies(q:str=''):
        combined={}
        for path in (ROOT/'data/resources/companies.sqlite3',store.path/'companies.sqlite3'):
            if not path.exists():
                continue
            for row in ResourceStore.read_existing(path):
                old=combined.get(row['company_name'])
                if old:
                    for key in ('industry','main_business','company_intro','official_url','career_url'):
                        if not row.get(key) and old.get(key):
                            row[key]=old[key]
                            row['field_status'][key]=old.get('field_status',{}).get(key,'unknown')
                    row['observations']=old['observations']+row['observations']
                combined[row['company_name']]=row
        return [r for r in combined.values() if q.casefold() in (r['company_name']+' '+str(r.get('industry') or '')).casefold()]

    @app.post('/api/browser/{action}')
    def browser_action(action:str,body:dict):
        if action not in ('open','tabs','capture','close','search_test','resume_site','check_login'):
            raise ValueError('不支持此浏览器操作')
        if action=='check_login':
            if body.get('channel') not in PLATFORMS:raise ValueError('未支持的登录平台')
            body={'channel':body['channel']}
        if action in ('open','resume_site'):
            validate_url(str(body.get('url','')))
        if action=='resume_site' and body.get('task_id'):
            target=store.get(body['task_id'])
            if target.get('historical'):raise ValueError('历史导入任务无需恢复')
        if action=='search_test':
            body={'query':'广州 财务BP 招聘'}
        if action=='capture':
            if not body.get('task_id'):
                raise ValueError('请先选择目标任务，读取次数将计入该任务')
            def reserve(t):
                if t.get('historical') or t['usage']['pages']>=t['input']['page_limit']:
                    raise ValueError('任务不可读取或已达到网页读取上限')
                t['usage']['pages']+=1
            store.update(body['task_id'],reserve)
        return {'command_id':store.command('browser_'+action,body)}

    @app.get('/api/commands/{cid}')
    def command_status(cid:str):
        with store.connect() as db:
            row=db.execute('SELECT * FROM commands WHERE id=?',(cid,)).fetchone()
        if not row:
            raise KeyError()
        return {'state':row['state'],'result':json.loads(row['result'])}

    @app.post('/api/tasks/{tid}/browser/import')
    def browser_import(tid:str,body:dict):
        with store.connect() as db:
            row=db.execute('SELECT body FROM commands WHERE id=?',(str(body.get('command_id','')),)).fetchone()
        if not row or json.loads(row['body']).get('task_id')!=tid:
            raise ValueError('此浏览器原文不属于所选任务，请重新读取')
        status=command_status(str(body.get('command_id','')))
        if status['state']!='done' or 'page' not in status['result']:
            raise ValueError('请先成功读取专用浏览器标签页')
        page=status['result']['page']
        def change(t):
            if t.get('historical'):
                raise ValueError('历史任务不能追加网页')
            add_candidates(t,[{'url':page['url'],'title':page['title']}],{'id':'browser_import','channel':body.get('channel','official_career'),'city':t['input']['cities'][0]})
            c=next(c for c in t['candidates'] if c['url']==page['url'])
            c['status']='pending_extract'
            t['events'].append({'at':now(),'message':'已取得浏览器原文，待模型提取：'+page['title'],'kind':'info'})
        task=store.update(tid,change)
        candidate=next(c for c in task['candidates'] if c['url']==page['url'])
        save_page(store,tid,candidate,page)
        return summary(store.get(tid))

    dist=ROOT/'web/dist'
    if dist.exists():
        app.mount('/',StaticFiles(directory=dist,html=True),name='ui')
    return app
