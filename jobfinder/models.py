from __future__ import annotations

from datetime import datetime, timezone, timedelta, date
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator, PrivateAttr
from .input_rules import recruitment_terms
from .education import EducationLevel, candidate_level, normalize_education

RULE_VERSION = "CN_MAINLAND-3.9"
SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")


def now() -> str:
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EducationConfig(Model):
    enabled: bool = True
    value: Literal["小学", "初中", "高中", "中专", "大专", "本科", "硕士", "博士"] | None = None
    mode: Literal["candidate_qualification", "posting_requirement"] | None = None
    comparison: Literal["exact", "at_most", "at_least"] = "exact"
    include_unrestricted: bool = False

    @model_validator(mode="before")
    @classmethod
    def simple_education(cls, data):
        # New shorthand has the user's chosen upper-threshold meaning. Explicit
        # legacy modes/comparisons retain their original semantics.
        if isinstance(data, str):
            value = data.strip()
            if value in ("不限", "学历不限"):
                return {"enabled": False}
            data = {"value": value}
        if isinstance(data, dict) and data.get("value") is not None and "mode" not in data and data.get("enabled", True):
            return {"mode": "posting_requirement", "comparison": "at_most", **data}
        return data

    @model_validator(mode="after")
    def comparison_mode(self):
        if not self.enabled:
            if self.value is not None or self.mode is not None or self.comparison != "exact" or self.include_unrestricted:
                raise ValueError("学历筛选不限时不能混填学历门槛、模式或比较选项")
            return self
        if self.value is None or self.mode is None:
            raise ValueError("学历筛选启用时须填写value及mode；不限须显式enabled=false")
        if self.mode != "posting_requirement" and (self.comparison != "exact" or self.include_unrestricted):
            raise ValueError("门槛比较及学历不限选项仅适用于posting_requirement")
        return self


class ExperienceConfig(Model):
    mode: Literal["candidate_qualification", "posting_requirement"]
    years: float = Field(ge=0, allow_inf_nan=False)
    comparison: Literal["exact", "at_most", "at_least"] = "at_most"
    include_unrestricted: bool = False

    @model_validator(mode="after")
    def comparison_mode(self):
        if self.mode == "candidate_qualification" and (self.comparison != "at_most" or self.include_unrestricted):
            raise ValueError("本人经验只比较能否满足岗位最低年限，不能混填岗位门槛选项")
        return self


class SalaryConfig(Model):
    enabled: bool = True
    amount: float | None = Field(default=None, gt=0)
    currency: str = "CNY"
    period: Literal["month", "year", "day", "hour"] | None = None
    basis: Literal["gross", "net"] | None = None
    match_mode: Literal["conservative", "possible"] = "conservative"

    @field_validator("currency")
    @classmethod
    def currency_code(cls, value):
        if not value.strip():
            raise ValueError("薪资币种不能为空")
        return value.strip().upper()

    @model_validator(mode="after")
    def salary_mode(self):
        if self.enabled and (any(x is None for x in (self.amount, self.period, self.basis)) or "currency" not in self.model_fields_set):
            raise ValueError("薪资筛选启用时须填写金额、币种、周期及税前税后口径；未填整个salary时不筛选")
        if not self.enabled and any(x is not None for x in (self.amount, self.period, self.basis)):
            raise ValueError("薪酬不限时不能混填金额、周期或税前税后条件")
        return self


class Budget(Model):
    searches_per_city: int = Field(default=10, ge=1, le=30)
    per_city: dict[str,int] = Field(default_factory=dict)
    companies_per_city: int | None = Field(default=None, ge=1, le=100)
    requests: int | None = Field(default=None, ge=0, le=100)
    timeout: float = Field(default=12, gt=0, le=30)
    retries: int = Field(default=1, ge=0, le=2)
    domain_interval: float = Field(default=1.0, ge=0.2)
    max_bytes: int = Field(default=2000000, ge=1000, le=10000000)

    @field_validator('per_city')
    @classmethod
    def city_bounds(cls,value):
        if any(type(n)!=int or not 1<=n<=1000 for n in value.values()) or sum(value.values())>5000:
            raise ValueError('城市搜索预算须在 1–1000 之间，合计不超过 5000')
        return value

    def city_searches(self,city):
        return self.per_city.get(city,self.searches_per_city)

    def city_requests(self, index, count, used=0):
        """None is unlimited; finite totals retain the existing city allocation."""
        if self.requests is None:
            return None
        return max(0, self.requests // count + (index < self.requests % count) - used)


class SearchConfig(Model):
    _effective_spec: dict | None = PrivateAttr(default=None)
    _raw_input: dict | None = PrivateAttr(default=None)
    # Missing versions are legacy: never reinterpret an existing config.
    rules_version: Literal['CN_MAINLAND-3.9','CN_MAINLAND-4.0'] = 'CN_MAINLAND-3.9'
    candidate_education: EducationLevel | None = None
    config_kind: Literal["user", "example", "offline", "legacy"] = "legacy"
    user_input_confirmed: bool = False
    location_mode: Literal["cities", "all_mainland"] = "cities"
    market_scope: Literal["CN_MAINLAND"] = "CN_MAINLAND"
    example_only: bool = False
    role_mode: Literal["keywords", "any"] = "keywords"
    role_keywords: list[str]
    company_scope_keywords: list[str] = Field(default_factory=list)
    target_cities: list[str] = Field(min_length=1)
    education: EducationConfig
    salary: SalaryConfig = Field(default_factory=lambda: SalaryConfig(enabled=False))
    candidate_experience_years: float | None = Field(default=None, ge=0)
    experience: ExperienceConfig | None = None
    recruitment_type: str | list[str] | None = None
    posted_after: date | None = None
    posted_before: date | None = None
    unknown_policy: Literal["review", "exclude"] = "review"
    remote_allowed: Literal[False] = False
    sources: list[str] = Field(default_factory=lambda: ["official_career", "manual_import"])
    budget: Budget = Field(default_factory=Budget)
    search_provider: Literal["agent_assisted", "codex_assisted", "unconfigured"] = "codex_assisted"

    @model_validator(mode='before')
    @classmethod
    def candidate_input(cls, data):
        if isinstance(data,dict) and data.get('rules_version')=='CN_MAINLAND-4.0':
            data=dict(data)
            value=candidate_level(data.get('candidate_education'))
            data['candidate_education']=value
            from .education import CORE_LABELS
            expected={'enabled':False} if value is None else {'value':CORE_LABELS[value],'mode':'candidate_qualification'}
            if 'education' in data and EducationConfig.model_validate(data['education']) != EducationConfig.model_validate(expected):
                raise ValueError('新规则学历只能由 candidate_education 生成，不可混填岗位门槛')
            data['education']=expected
        elif isinstance(data,dict) and data.get('candidate_education') is not None:
            raise ValueError('我的学历需要明确 rules_version=CN_MAINLAND-4.0')
        return data

    @field_validator("recruitment_type")
    @classmethod
    def hiring_labels(cls, value):
        if value is None:
            return None
        values = [value] if isinstance(value, str) else value
        if not values or any(not recruitment_terms(item) for item in values):
            raise ValueError("招聘类型不能留空字符串/空列表；不筛选请省略或设null")
        labels = recruitment_terms(value)
        return labels[0] if isinstance(value, str) and len(labels) == 1 else labels

    @model_validator(mode="after")
    def nonempty(self):
        if self.experience is not None and self.candidate_experience_years is not None:
            raise ValueError("experience与旧candidate_experience_years不能同时填写")
        if self.role_mode == "keywords" and not self.role_keywords:
            raise ValueError("岗位关键词不能为空；不限职位须显式role_mode=any并提供企业范围")
        if self.role_mode == "any" and (self.role_keywords or not self.company_scope_keywords):
            raise ValueError("不限职位须清空岗位关键词，并提供非空company_scope_keywords")
        if any(not s.strip() for s in self.company_scope_keywords):
            raise ValueError("企业范围关键词不能为空白")
        if self.location_mode == "all_mainland":
            raise ValueError("本阶段不支持全国检索模式，请显式填写目标城市；空列表不代表全国")
        if self.config_kind == "example" and not self.example_only:
            raise ValueError("演示配置不能仅删除 example_only 标记转为用户条件")
        if any(not s.strip() for s in self.role_keywords + self.target_cities):
            raise ValueError("岗位和城市不能为空")
        from .geography import normalize_city
        if any(normalize_city(s) is None for s in self.target_cities):
            raise ValueError("目标城市未在已审核大陆城市别名表中，请扩充并测试映射，不能猜测")
        if self.posted_after and self.posted_before and self.posted_after > self.posted_before:
            raise ValueError("发布日期范围倒置")
        return self


class Evidence(Model):
    source_url: str
    retrieved_at: str = Field(default_factory=now)
    content_hash: str
    method: str
    snapshot: str | None = None
    field_quotes: dict[str, str] = Field(default_factory=dict)
    untrusted_content: Literal[True] = True


class Company(Model):
    company_intro: str | None = None
    main_business: str | None = None
    industry: str | None = None
    coverage_scope: str | None = None
    list_date: str | None = None
    acquired_at: str | None = None
    known_limitations: list[str] = Field(default_factory=list)
    legal_name: str
    listed_status: bool | None = None
    soe_status: str | None = None
    group_relation: str | None = None
    registered_location: str | None = None
    headquarters_location: str | None = None
    listing_location: str | None = None
    official_url: str | None = None
    career_url: str | None = None
    source_url: str
    version_date: str
    discovery_kind: Literal["local_company", "outside_company_local_jobs", "unknown"] = "unknown"
    notes: str | None = None


class Candidate(Model):
    source_type: Literal['official_website', 'recruitment_provider', 'platform', 'repost', 'unknown'] = 'unknown'
    source_evidence: dict = Field(default_factory=dict)
    entry_task_id: str | None = None
    announcement_url: str | None = None
    company_profile_url: str | None = None
    pagination_urls: list[str] = Field(default_factory=list)
    role_decisions: list[dict] = Field(default_factory=list)
    navigation_resources: list[str] = Field(default_factory=list, max_length=2)
    navigation_urls: list[str] = Field(default_factory=list, max_length=2)
    original_discovered_url: str | None = None
    task_ids: list[str] = Field(default_factory=list)
    target_cities: list[str] = Field(default_factory=list)
    candidate_type: Literal["company_home", "career_entry", "announcement", "job_detail", "enterprise_directory", "unknown"] = "unknown"
    processing_status: str = "pending"
    discovery_source: str | None = None
    query: str
    url: str
    title: str
    discovered_at: str
    source_channel: str
    company: str | None = None
    official_url: str | None = None
    career_url: str | None = None
    local_file: str | None = None
    authorized_import: bool = False
    synthetic: bool = False


class Job(Model):
    minimum_education: EducationLevel = EducationLevel.UNKNOWN

    def __setattr__(self,name,value):
        super().__setattr__(name,value)
        if name=='education_raw':
            super().__setattr__('minimum_education',normalize_education(value).minimum)
    evidence_level: Literal['search_snippet', 'onsite_list', 'original_detail'] | None = None
    detail_url: str | None = None
    role_review: dict | None = None
    source_type: Literal['official_website', 'recruitment_provider', 'platform', 'repost', 'unknown'] = 'unknown'
    details_complete: bool = True
    location_codes_raw: list[str] = Field(default_factory=list)
    discovery_task_ids: list[str] = Field(default_factory=list)
    discovery_cities: list[str] = Field(default_factory=list)
    validity_checked_at: str | None = None
    company: str
    title: str
    description: str
    requirements: str | None = None
    location_raw: str | None = None
    locations: list[str] = Field(default_factory=list)
    location_mode: Literal["single", "alternatives", "rotation", "unknown"] = "single"
    work_mode: Literal["onsite", "hybrid", "remote", "unknown"] = "unknown"
    education_raw: str | None = None
    experience_raw: str | None = None
    salary_raw: str | None = None
    salary_currency: str | None = None
    salary_period: str | None = None
    salary_basis: str | None = None
    salary_component: Literal["fixed", "unknown"] = "unknown"
    recruitment_type: str | None = None
    posted_at: str | None = None
    deadline: str | None = None
    open_status: Literal["OPEN", "CLOSED", "UNKNOWN"] = "UNKNOWN"
    application_method: str | None = None
    application_url: str | None = None
    source_url: str
    source_channel: str
    source_id: str | None = None
    level: str | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    conflicts: dict[str, list[str]] = Field(default_factory=dict)
    synthetic: bool = False

    @model_validator(mode='after')
    def normalize_facts(self):
        # Never trust model-provided normalized levels without their raw source.
        self.minimum_education=normalize_education(self.education_raw).minimum
        return self


class Decision(Model):
    state: Literal["PASS", "FAIL", "UNKNOWN"]
    reason: str


class GeoDecision(Decision):
    raw: str | None
    city: str | None = None
    evidence: str
    rule_version: str = RULE_VERSION


class MatchResult(Model):
    matched_cities: list[str] = Field(default_factory=list)
    job: Job
    original_job_key: str
    geo: GeoDecision
    dimensions: dict[str, Decision]
    status: Literal["MATCH", "REVIEW", "EXCLUDED"]
    pending: list[str]
    checked_at: str = Field(default_factory=now)
    rule_version: str = RULE_VERSION
