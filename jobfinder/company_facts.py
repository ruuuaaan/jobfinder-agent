"""Evidence contracts, not a semantic classifier or a web crawler."""
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit
from pydantic import Field, model_validator
from .role_expansion import Record, Text
from .geography import normalize_city
import unicodedata


class SourceEvidence(Record):
    entity_name: Text
    source_url: Text
    observed_at: Text
    quote: Text
    content_hash: Text
    kind: Literal['profile', 'job', 'operating_unit', 'explicit_scope']
    subject_relation: Literal['self', 'operating_unit', 'parent', 'subsidiary', 'headquarters', 'registration'] = 'self'

    @model_validator(mode='after')
    def check_source(self):
        date = datetime.fromisoformat(self.observed_at)
        if date.tzinfo is None:
            raise ValueError('证据时间须含时区')
        url = urlsplit(self.source_url)
        if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password:
            raise ValueError('证据须有原始网页来源URL')
        return self


class OperatingLocation(Record):
    city: Text
    unit_name: Text
    kind: Literal['office', 'branch', 'factory', 'store', 'research_center', 'operating_site']

    @model_validator(mode='after')
    def normalize(self):
        city = normalize_city(self.city)
        if not city:
            raise ValueError('经营机构城市必须明确，模糊地点保留未知而非猜测')
        self.city = city
        return self


class HiringRecord(Record):
    title: Text
    cities: list[Text] = Field(default_factory=list)

    @model_validator(mode='after')
    def explicit_cities(self):
        cities = [normalize_city(c) for c in self.cities]
        if any(not c for c in cities):
            raise ValueError('历史岗位城市须明确；原文地点未知时cities=[]')
        self.cities = list(dict.fromkeys(cities))
        return self


STRUCTURED_FIELDS = {'operating_locations': OperatingLocation, 'hiring_history': HiringRecord}
PROFILE_FIELDS = ('company_intro', 'main_business', 'industry', 'operating_locations')


def normalize_value(field, value):
    if field in STRUCTURED_FIELDS:
        if not isinstance(value, list):
            raise ValueError(f'{field}须为结构化数组')
        return [STRUCTURED_FIELDS[field].model_validate(v).model_dump() for v in value]
    if not isinstance(value, str):
        raise ValueError(f'{field}须为文字')
    return value.strip()


def combine_locations(records):
    """Different operating units coexist; conflicting cities for one named unit need review."""
    units = {}
    for record in records:
        item = OperatingLocation.model_validate(record).model_dump()
        key = (''.join(unicodedata.normalize('NFKC',item['unit_name']).split()).casefold(), item['kind'])
        if key in units and units[key]['city'] != item['city']:
            return None
        units[key] = item
    return [units[k] for k in sorted(units)]


class CompanyFact(Record):
    company_name: Text
    field: Literal['company_intro', 'main_business', 'industry', 'operating_locations', 'hiring_history']
    value: str | list[dict]
    evidence: SourceEvidence

    @model_validator(mode='after')
    def check_owner(self):
        from .resources import name_key
        if name_key(self.company_name) != name_key(self.evidence.entity_name):
            raise ValueError('资料主体必须与入库企业相同，集团/子公司不可互相继承')
        allowed = ('self', 'operating_unit') if self.field == 'operating_locations' else ('self',)
        if self.evidence.subject_relation not in allowed:
            raise ValueError('注册地、总部或关联主体证据不能代替本企业经营资料')
        if self.field == 'operating_locations' and self.evidence.kind != 'operating_unit':
            raise ValueError('实际经营机构须有经营机构原文证据')
        if self.field == 'hiring_history' and self.evidence.kind != 'job':
            raise ValueError('招聘历史须有具体岗位原文证据')
        self.value = normalize_value(self.field, self.value)
        return self


class CompanyFacts(Record):
    schema_version: Literal['company-facts-1']
    artifact_kind: Literal['example', 'user']
    facts: list[CompanyFact]


def record_facts(store, batch):
    batch = CompanyFacts.model_validate(batch)
    if batch.artifact_kind != 'user':
        raise ValueError('示例资料不得写入真实企业库')
    ids = []
    for fact in batch.facts:
        evidence = fact.evidence.model_dump()
        ids.append(store.add(fact.company_name, evidence['source_url'], evidence['observed_at'],
                             'assistant_original_profile', values={fact.field: fact.value},
                             supported=(fact.field,), evidence=evidence))
    return list(dict.fromkeys(ids))
