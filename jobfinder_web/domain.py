from __future__ import annotations
import hashlib
import itertools
from copy import deepcopy
from typing import Literal
from urllib.parse import urlparse
from pydantic import BaseModel, Field, ConfigDict, model_validator, PrivateAttr
from jobfinder.models import SearchConfig, now
from jobfinder.spec import JobSearchSpec, ProviderPolicy, task_contract
from jobfinder.education import candidate_level, EducationLevel

CHANNELS = {
    'official_career': {'name':'企业官网', 'hint':'招聘 官网', 'domains':[]},
    'zhaopin': {'name':'智联招聘', 'hint':'site:zhaopin.com 招聘', 'domains':['zhaopin.com']},
    'boss': {'name':'BOSS直聘', 'hint':'site:zhipin.com 招聘', 'domains':['zhipin.com']},
    'liepin': {'name':'猎聘', 'hint':'site:liepin.com 招聘', 'domains':['liepin.com']},
    '51job': {'name':'前程无忧', 'hint':'site:51job.com 招聘', 'domains':['51job.com']},
    'talent_platform': {'name':'人才平台', 'hint':'人才网 招聘会 职位', 'domains':[]},
    'public_hr': {'name':'人社公告', 'hint':'site:gov.cn 招聘', 'domains':[]},
    'bendibao_web': {'name':'本地宝', 'hint':'site:bendibao.com 招聘', 'domains':['bendibao.com']},
    'wechat': {'name':'微信公众号', 'hint':'site:mp.weixin.qq.com 招聘', 'domains':['mp.weixin.qq.com']},
    'campus': {'name':'高校就业网', 'hint':'site:edu.cn 就业 招聘', 'domains':[]},
}


class TaskInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    cities: list[str] = Field(min_length=1, max_length=10)
    role: str = Field(min_length=1, max_length=120)
    terms: list[str] = Field(default_factory=list, max_length=40)
    adjacent_terms: list[str] = Field(default_factory=list, max_length=40)
    education: str | None = None  # Legacy input only; never relabel as candidate education.
    candidate_education: EducationLevel | None = None
    rules_version: Literal['CN_MAINLAND-4.0','CN_MAINLAND-3.9'] | None = None
    allowed_providers: list[Literal['model','browser','tavily']] | None = None
    provider_policy: ProviderPolicy = Field(default_factory=ProviderPolicy)
    _raw_input: dict = PrivateAttr(default_factory=dict)
    notes: str = Field(default='', max_length=2000)
    channels: list[str] = Field(min_length=1)
    search_mode: Literal['auto','model','browser','tavily'] = 'auto'
    onsite_search: bool = False  # Existing API clients retain their explicit route.
    city_budgets: dict[str, int]
    channel_budgets: dict[str, int] = Field(default_factory=dict)
    # Missing on legacy API inputs/tasks: keep the original shared allowance.
    budget_policy: Literal['shared','official_priority_once'] = 'shared'
    page_limit: int = Field(default=1000, ge=1, le=2000)
    model_call_limit: int = Field(default=1000, ge=1, le=2500)
    time_limit_minutes: int = Field(default=60, ge=1, le=1440)
    experience_years: float | None = Field(default=None, ge=0, le=60)
    recruitment_type: str | list[str] | None = None
    salary_amount: float | None = Field(default=None, gt=0)
    salary_basis: str = 'gross'

    @model_validator(mode='wrap')
    @classmethod
    def capture_raw(cls,data,handler):
        raw=deepcopy(data) if isinstance(data,dict) else None
        if isinstance(data,dict):
            data=dict(data)
            data['candidate_education']=candidate_level(data.get('candidate_education'))
        obj=handler(data)
        if raw is not None:obj._raw_input=raw
        return obj

    @model_validator(mode='after')
    def valid(self):
        if self.education is not None:
            if self.candidate_education is not None or self.rules_version=='CN_MAINLAND-4.0':
                raise ValueError('旧版岗位学历筛选不能与我的学历混填')
            self.rules_version='CN_MAINLAND-3.9'
        elif self.rules_version=='CN_MAINLAND-3.9':
            raise ValueError('旧任务必须提供原学历规则')
        else:self.rules_version='CN_MAINLAND-4.0'
        if self.allowed_providers is None:
            self.allowed_providers=['model','browser'] if self.search_mode=='auto' else [self.search_mode]
        if not self.allowed_providers or (self.search_mode!='auto' and self.search_mode not in self.allowed_providers):
            raise ValueError('请至少启用所选搜索服务')
        self.cities = list(dict.fromkeys(x.strip() for x in self.cities))
        self.channels = list(dict.fromkeys(self.channels))
        self.terms = list(dict.fromkeys(x.strip() for x in (self.terms or [self.role]) if x.strip()))
        if not self.role.strip() or not self.terms or any(len(t)>120 for t in self.terms+self.adjacent_terms):
            raise ValueError('请填写有效岗位词')
        if set(self.channels)-CHANNELS.keys():
            raise ValueError('渠道未支持')
        if set(self.city_budgets) != set(self.cities) or any(type(n)!=int or n<1 or n>1000 for n in self.city_budgets.values()):
            raise ValueError('请为每座城市设置 1–1000 次预算')
        if sum(self.city_budgets.values())>5000:
            raise ValueError('单任务搜索上限为5000次')
        if set(self.channel_budgets)-set(self.channels) or any(type(n)!=int or n<0 or n>5000 for n in self.channel_budgets.values()):
            raise ValueError('渠道预算无效')
        from .channel_budget import validate_allocation
        validate_allocation(self.cities,self.channels,self.city_budgets,self.channel_budgets,self.budget_policy)
        self.config()
        return self

    def config(self):
        legacy_alias={'本科及以上':'at_least_bachelor','本科及以下':'at_most_bachelor','仅本科门槛':'bachelor','硕士及以上':'at_least_master','不限':'any'}
        education = {
            'any': {'enabled':False},
            'at_least_bachelor': {'value':'本科','mode':'posting_requirement','comparison':'at_least'},
            'at_most_bachelor': {'value':'本科','mode':'posting_requirement','comparison':'at_most','include_unrestricted':True},
            'bachelor': {'value':'本科','mode':'posting_requirement','comparison':'exact'},
            'at_least_master': {'value':'硕士','mode':'posting_requirement','comparison':'at_least'},
        }.get(legacy_alias.get(self.education,self.education))
        if education is None and self.education is not None:
            raise ValueError('学历选项无效')
        args={'rules_version':self.rules_version or 'CN_MAINLAND-4.0','candidate_education':self.candidate_education}
        if education is not None:args['education']=education
        return SearchConfig(**args,config_kind='user', user_input_confirmed=True, target_cities=self.cities,
            role_keywords=self.terms or [self.role], sources=self.channels,
            experience={'mode':'candidate_qualification','years':self.experience_years} if self.experience_years is not None else None,
            recruitment_type=self.recruitment_type or None,
            salary={'enabled':True,'amount':self.salary_amount,'currency':'CNY','period':'month','basis':self.salary_basis} if self.salary_amount else {'enabled':False})

    def spec(self):
        return JobSearchSpec.from_config(self.config(),self.model_dump(mode='json'))


def make_queries(inp: TaskInput):
    queries = []
    terms = list(dict.fromkeys(inp.terms + inp.adjacent_terms))
    # Cover aliases with OR, never turn synonyms into simultaneous requirements.
    # Bounded groups also obey the native search provider's 128-character limit.
    for channel,city in itertools.product(inp.channels,inp.cities):
        if inp.onsite_search and channel=='zhaopin':
            text=f'{city} {inp.terms[0]} 站内搜索'
            queries.append({'id':hashlib.sha256(text.encode()+channel.encode()).hexdigest()[:20],
                'city':city,'channel':channel,'query':text,'terms':[inp.terms[0]],'kind':'onsite','status':'pending'})
            continue
        group=[]
        for term in (inp.terms if channel in ('boss','liepin','51job') else terms):
            proposed=f'{city} ('+' OR '.join('"'+t+'"' for t in group+[term])+f') {CHANNELS[channel]["hint"]}'
            if len(proposed)>128:break
            group.append(term)
        if not group:group=[terms[0]]
        query=f'{city} ('+' OR '.join('"'+t+'"' for t in group)+f') {CHANNELS[channel]["hint"]}'
        queries.append({'id':hashlib.sha256(query.encode()).hexdigest()[:20],'city':city,'channel':channel,
            'query':query,'terms':group,'kind':'index','status':'pending',
            'route_reason':'优先公开索引；站内补充需剩余预算' if inp.onsite_search and channel in ('boss','liepin','51job') else '首轮渠道覆盖'})
    # After covering channels, additional budget may test site search. Every
    # attempt, including its fallback, spends the same city/channel allowance.
    if inp.onsite_search:
        for channel,city in itertools.product([c for c in inp.channels if c in ('boss','liepin','51job')],inp.cities):
            text=f'{city} {inp.terms[0]} 站内搜索'
            queries.append({'id':hashlib.sha256(text.encode()+channel.encode()).hexdigest()[:20],
                'city':city,'channel':channel,'query':text,'terms':[inp.terms[0]],'kind':'onsite','status':'pending','route_reason':'首轮覆盖后补充站内结果'})
    variants = ['', ' 岗位职责 任职要求', ' 招聘信息 投递', ' 社会招聘 近期']
    for suffix, term, channel, city in itertools.product(variants, terms, inp.channels, inp.cities):
        query = f'{city} {term} {CHANNELS[channel]["hint"]}{suffix}'
        qid = hashlib.sha256(query.encode()).hexdigest()[:20]
        if not any(q['id']==qid for q in queries):
            queries.append({'id':qid,'city':city,'channel':channel,'query':query,'terms':[term],'kind':'index','status':'pending'})
    return queries


def new_task(inp):
    return task_contract({'input':inp.model_dump(mode='json'), 'raw_input':deepcopy(inp._raw_input),
        'effective_search_spec':inp.spec().model_dump(mode='json'),'config':inp.config().model_dump(mode='json'), 'state':'draft',
        'queries':make_queries(inp), 'candidates':[], 'jobs':[], 'events':[],
        'budget_revisions':[{'at':now(),'cities':inp.city_budgets,'channels':inp.channel_budgets}],
        'usage':{'searches':0,'pages':0,'model_calls':0,'input_tokens':0,'output_tokens':0,'search_credits':0,'active_seconds':0},
        'connections':{}, 'skill_version':'web-2 / '+inp.rules_version, 'historical':False,
        'quality_version':2,'execution_policy_version':2,'config_purpose':'matching_only', 'budget_authority':'effective_search_spec + query attempts'})


def query_coverage(task):
    from .search_execution import attempt_count
    rows=[]
    for city,channel in itertools.product(task['input']['cities'],task['input']['channels']):
        attempted=[q for q in task['queries'] if q['city']==city and q['channel']==channel and attempt_count(q)]
        covered=list(dict.fromkeys(term for q in attempted for term in q.get('terms',[])))
        rows.append({'city':city,'channel':channel,'attempted_terms':covered,
            'not_run_terms':[t for t in task['input']['terms']+task['input']['adjacent_terms'] if t not in covered],
            'attempts':sum(attempt_count(q) for q in attempted),'completed':sum(q['status']=='done' for q in attempted)})
    return rows


def available_query(task):
    from .search_execution import next_query
    return next_query(task)


def schedule_index_fallback(task, failed):
    if failed.get('kind')!='onsite':return
    if any(q.get('fallback_of')==failed['id'] for q in task['queries']):return
    query=f'{failed["city"]} {failed["terms"][0]} {CHANNELS[failed["channel"]]["hint"]}'
    task['queries'].append({'id':hashlib.sha256((failed['id']+'-fallback').encode()).hexdigest()[:20],
        'city':failed['city'],'channel':failed['channel'],'query':query,'terms':[failed['terms'][0]],
        'kind':'index','status':'pending','fallback_of':failed['id'],'route_reason':'站内未取得结果，使用公开索引查找公开线索'})
    task['events'].append({'at':now(),'kind':'info','message':'已安排 '+CHANNELS[failed['channel']]['name']+' 公开索引回退；仍受累计城市和渠道预算限制，额度不足时保留待办'})


def source_channel(url, route):
    host = (urlparse(url).hostname or '').lower()
    for key, data in CHANNELS.items():
        if any(host==d or host.endswith('.'+d) for d in data['domains']):
            return key
    if host.endswith('.gov.cn'):
        return 'public_hr'
    if host.endswith('.edu.cn'):
        return 'campus'
    # The discovery route is not sufficient evidence of official employer identity.
    return 'unverified_website'


def role_suggestions(role):
    if any(x in role.casefold() for x in ['财务bp','财务业务伙伴','finance business partner']):
        return {'terms':[role,'财务业务伙伴','Finance Business Partner'], 'adjacent_terms':['业务财务','财务分析','经营分析'], 'method':'内置岗位词建议；相邻岗位须核验职责'}
    if '动画' in role:
        return {'terms':[role,'动画师','动画制作'], 'adjacent_terms':['三维动画师','二维动画师','动画分镜','角色绑定'], 'method':'内置岗位词建议；相邻岗位须核验职责'}
    return {'terms':[role], 'adjacent_terms':[], 'method':'保留原始岗位词；连接模型后可生成相关岗位'}
