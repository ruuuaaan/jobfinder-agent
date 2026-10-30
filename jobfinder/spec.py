"""Canonical saved search contract shared by CLI, web, prompts and matching.

SearchConfig is the compatibility projection used by the existing rule engine.
Unversioned inputs retain the complete legacy config, never guessed candidate data.
"""
from copy import deepcopy
from datetime import date
from typing import Literal
from pydantic import Field, model_validator, field_validator
from .models import Model, SearchConfig, ExperienceConfig, SalaryConfig, Budget
from .education import EducationLevel, candidate_level, LABELS, RULES_VERSION, LEGACY_RULES_VERSION
from .input_rules import recruitment_terms


class JobScope(Model):
    mode: Literal['keywords','any'] = 'keywords'
    terms: list[str] = Field(default_factory=list)
    adjacent_terms: list[str] = Field(default_factory=list)
    company_scope: list[str] = Field(default_factory=list)
    job_groups: dict = Field(default_factory=dict)


class SearchBudget(Model):
    per_city: dict[str,int] = Field(default_factory=dict)
    per_channel: dict[str,int] = Field(default_factory=dict)
    allocation_policy: Literal['shared','official_priority_once'] = 'shared'
    per_city_channel: dict[str,dict[str,int]] = Field(default_factory=dict)

    @model_validator(mode='after')
    def bounds(self):
        if any(type(x)!=int or not 1<=x<=1000 for x in self.per_city.values()) or sum(self.per_city.values())>5000:
            raise ValueError('城市预算须为 1–1000，合计不超过 5000')
        if any(type(x)!=int or not 0<=x<=5000 for x in self.per_channel.values()):
            raise ValueError('渠道预算须为 0–5000')
        return self


class ProviderPolicy(Model):
    transient_retries: int = Field(default=1,ge=0,le=2)
    failure_threshold: int = Field(default=3,ge=1,le=10)
    cooldown_seconds: int = Field(default=60,ge=1,le=3600)
    retry_delay_seconds: int = Field(default=3,ge=0,le=60)
    max_recovery_probes: int = Field(default=1,ge=0,le=3)


class JobSearchSpec(Model):
    schema_version: Literal['job-search-spec-1'] = 'job-search-spec-1'
    rules_version: Literal['CN_MAINLAND-4.0','CN_MAINLAND-3.9'] = RULES_VERSION
    target_locations: list[str] = Field(min_length=1)
    job_scope: JobScope
    candidate_education: EducationLevel | None = None
    candidate_experience: ExperienceConfig | None = None
    salary_requirement: SalaryConfig = Field(default_factory=lambda:SalaryConfig(enabled=False))
    employment_types: list[str] = Field(default_factory=list)
    channels: list[str] = Field(default_factory=lambda:['official_career','manual_import'])
    search_budget: SearchBudget = Field(default_factory=SearchBudget)
    page_budget: int | None = Field(default=None,ge=1,le=2000)
    model_budget: int | None = Field(default=None,ge=1,le=2500)
    runtime_budget: int | None = Field(default=None,ge=1,le=1440,description='累计运行分钟')
    additional_notes: str = ''
    search_mode: Literal['auto','model','browser','tavily','agent_assisted','codex_assisted'] = 'codex_assisted'
    allowed_providers: list[Literal['model','browser','tavily']] = Field(default_factory=list)
    provider_policy: ProviderPolicy = Field(default_factory=ProviderPolicy)
    onsite_search: bool = False
    # Existing CLI controls remain intact; the web does not reinterpret them.
    core_budget: Budget = Field(default_factory=Budget)
    config_kind: Literal['user','example','offline','legacy'] = 'legacy'
    user_input_confirmed: bool = False
    example_only: bool = False
    posted_after: date | None = None
    posted_before: date | None = None
    unknown_policy: Literal['review','exclude'] = 'review'
    search_provider: Literal['agent_assisted','codex_assisted','unconfigured'] = 'codex_assisted'
    legacy_matching_config: dict | None = None

    @field_validator('candidate_education',mode='before')
    @classmethod
    def degree(cls,v):return candidate_level(v)

    @model_validator(mode='after')
    def valid(self):
        self.target_locations=list(dict.fromkeys(x.strip() for x in self.target_locations))
        self.employment_types=recruitment_terms(self.employment_types)
        self.channels=list(dict.fromkeys(self.channels))
        self.allowed_providers=list(dict.fromkeys(self.allowed_providers))
        if not self.search_budget.per_city:
            self.search_budget.per_city={city:self.core_budget.searches_per_city for city in self.target_locations}
        if set(self.search_budget.per_city)!=set(self.target_locations) or set(self.search_budget.per_channel)-set(self.channels):
            raise ValueError('预算必须对应本轮城市和渠道')
        from .search_budget import PRIORITY, validate_allocation, base_allocation
        validate_allocation(self.target_locations,self.channels,self.search_budget.per_city,self.search_budget.per_channel,self.search_budget.allocation_policy)
        self.search_budget.per_city_channel=(base_allocation(self.target_locations,self.channels,self.search_budget.per_city)
            if self.search_budget.allocation_policy==PRIORITY else {})
        if self.rules_version==LEGACY_RULES_VERSION:
            if self.legacy_matching_config is None or self.candidate_education is not None:
                raise ValueError('旧任务必须保存原匹配配置，不能推定本人学历')
        elif self.legacy_matching_config is not None:
            raise ValueError('新规则不能混入旧版匹配配置')
        self.to_config()
        return self

    def to_config(self):
        if self.rules_version==LEGACY_RULES_VERSION:
            return SearchConfig.model_validate(deepcopy(self.legacy_matching_config))
        return SearchConfig(rules_version=self.rules_version,candidate_education=self.candidate_education,
            config_kind=self.config_kind,user_input_confirmed=self.user_input_confirmed,example_only=self.example_only,
            target_cities=self.target_locations,role_mode=self.job_scope.mode,role_keywords=self.job_scope.terms,
            company_scope_keywords=self.job_scope.company_scope,sources=self.channels,
            salary=self.salary_requirement,experience=self.candidate_experience,
            recruitment_type=self.employment_types or None,budget=self.core_budget.model_copy(update={'per_city':{k:v for k,v in self.search_budget.per_city.items() if v!=self.core_budget.searches_per_city}}),
            posted_after=self.posted_after,posted_before=self.posted_before,unknown_policy=self.unknown_policy,search_provider=self.search_provider)

    @classmethod
    def from_config(cls,config,execution=None):
        x=execution or {};c=config
        legacy=c.rules_version==LEGACY_RULES_VERSION
        return cls(rules_version=c.rules_version,target_locations=c.target_cities,
            job_scope=JobScope(mode=c.role_mode,terms=c.role_keywords,company_scope=c.company_scope_keywords,adjacent_terms=x.get('adjacent_terms',[])),
            candidate_education=None if legacy else c.candidate_education,
            candidate_experience=c.experience or (ExperienceConfig(mode='candidate_qualification',years=c.candidate_experience_years) if c.candidate_experience_years is not None else None),
            salary_requirement=c.salary,employment_types=recruitment_terms(c.recruitment_type),channels=c.sources,
            search_budget=SearchBudget(per_city=x.get('city_budgets') or {city:c.budget.city_searches(city) for city in c.target_cities},per_channel=x.get('channel_budgets',{}),allocation_policy=x.get('budget_policy','shared')),
            page_budget=x.get('page_limit'),model_budget=x.get('model_call_limit'),runtime_budget=x.get('time_limit_minutes'),
            additional_notes=x.get('notes',''),search_mode=x.get('search_mode','tavily') if execution is not None else 'codex_assisted',
            allowed_providers=x.get('allowed_providers') or [],provider_policy=x.get('provider_policy') or {},onsite_search=x.get('onsite_search',False),
            core_budget=c.budget,config_kind=c.config_kind,user_input_confirmed=c.user_input_confirmed,example_only=c.example_only,
            posted_after=c.posted_after,posted_before=c.posted_before,unknown_policy=c.unknown_policy,search_provider=c.search_provider,
            legacy_matching_config=c.model_dump(mode='json') if legacy else None)

    def education_summary(self):
        if self.rules_version==LEGACY_RULES_VERSION:
            e=self.to_config().education
            if not e.enabled:return '旧版条件：不筛选学历（保留创建时规则）'
            if e.mode=='candidate_qualification':return f'旧版条件：本人学历 {e.value}（保留创建时规则）'
            direction={'exact':'恰为','at_most':'不高于','at_least':'至少为'}[e.comparison]
            return f'旧版条件：岗位最低学历门槛{direction}{e.value}；'+('包含' if e.include_unrestricted else '不包含')+'学历不限'
        return '我的学历：'+(LABELS[self.candidate_education]+'；自动判断我是否满足岗位最低学历要求' if self.candidate_education else '未设置；不以学历筛选岗位')


def matching_config(value):
    if isinstance(value,JobSearchSpec):
        config=value.to_config();config._effective_spec=value.model_dump(mode='json')
        return config
    if isinstance(value,SearchConfig):return value
    if isinstance(value,dict) and value.get('schema_version')=='job-search-spec-1':
        config=matching_config(JobSearchSpec.model_validate(value))
    else:config=SearchConfig.model_validate(value)
    config._raw_input=deepcopy(value)
    return config


def spec_for_config(value):
    if isinstance(value,JobSearchSpec):return value
    config=matching_config(value)
    return JobSearchSpec.model_validate(config._effective_spec) if config._effective_spec else JobSearchSpec.from_config(config)


def effective_spec(task):
    if task.get('effective_search_spec'):
        return JobSearchSpec.model_validate(task['effective_search_spec'])
    return JobSearchSpec.from_config(SearchConfig.model_validate(task['config']),task['input'])


def task_contract(task):
    """Read-through legacy migration; persisted on the next ordinary task update."""
    task.setdefault('raw_input_provenance','saved_request' if 'raw_input' in task else 'legacy_saved_input')
    task.setdefault('raw_input',deepcopy(task['input']))
    spec=effective_spec(task)
    task['effective_search_spec']=spec.model_dump(mode='json')
    task['rules_version']=spec.rules_version
    task['education_summary']=spec.education_summary()
    if spec.rules_version==RULES_VERSION:
        # Existing worker modules consume a projection, never reinterpret raw input.
        task['config']=spec.to_config().model_dump(mode='json')
        task['input'].update(cities=spec.target_locations,terms=spec.job_scope.terms,adjacent_terms=spec.job_scope.adjacent_terms,
            candidate_education=spec.candidate_education.value if spec.candidate_education else None,channels=spec.channels,
            notes=spec.additional_notes,city_budgets=spec.search_budget.per_city,channel_budgets=spec.search_budget.per_channel,budget_policy=spec.search_budget.allocation_policy,
            page_limit=spec.page_budget,model_call_limit=spec.model_budget,time_limit_minutes=spec.runtime_budget,
            search_mode=spec.search_mode,allowed_providers=spec.allowed_providers,onsite_search=spec.onsite_search)
        task['input'].update(rules_version=spec.rules_version,provider_policy=spec.provider_policy.model_dump(),
            experience_years=spec.candidate_experience.years if spec.candidate_experience else None,
            salary_amount=spec.salary_requirement.amount if spec.salary_requirement.enabled else None,
            salary_basis=spec.salary_requirement.basis or 'gross',recruitment_type=spec.employment_types or None)
    return task


def revise_execution(task, changes):
    """Explicit execution-control edits update the contract, never original input."""
    spec=effective_spec(task).model_dump(mode='json')
    for key,target in {'page_limit':'page_budget','model_call_limit':'model_budget','time_limit_minutes':'runtime_budget',
        'search_mode':'search_mode','allowed_providers':'allowed_providers','onsite_search':'onsite_search','provider_policy':'provider_policy'}.items():
        if key in changes:spec[target]=changes[key]
    for key,target in {'city_budgets':'per_city','channel_budgets':'per_channel','budget_policy':'allocation_policy'}.items():
        if key in changes:spec['search_budget'][target]=changes[key]
    task['effective_search_spec']=JobSearchSpec.model_validate(spec).model_dump(mode='json')
    return task_contract(task)
