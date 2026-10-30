"""Evidence-backed, deterministic employer selection. No fetching or semantic inference."""
import hashlib
import json
from copy import deepcopy
from typing import Literal
from pydantic import Field, model_validator
from .company_facts import SourceEvidence, normalize_value, combine_locations
from .geography import normalize_city
from .resources import name_key
from .role_expansion import Record, Text, RoleExpansion, RoleNature


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class Assessment(Record):
    company_name: Text
    group_id: Text
    target_city: Text
    dimension: Literal['role', 'business', 'city']
    verdict: Literal['related', 'out_of_scope']
    rationale: Text
    coverage: Literal['partial', 'explicit_scope'] = 'partial'
    evidence: list[SourceEvidence] = Field(min_length=1)

    @model_validator(mode='after')
    def attribution(self):
        self.target_city = normalize_city(self.target_city)
        if not self.target_city:
            raise ValueError('企业判断必须关联明确的目标城市')
        for e in self.evidence:
            if name_key(e.entity_name) != name_key(self.company_name):
                raise ValueError('筛选证据必须属于本企业，不能继承集团/子公司资料')
            if e.subject_relation not in (('self', 'operating_unit') if self.dimension == 'city' else ('self',)):
                raise ValueError('总部、注册地或关联企业不能替代岗位/业务/经营城市证据')
            if self.verdict == 'out_of_scope':
                if self.coverage != 'explicit_scope' or e.kind != 'explicit_scope':
                    raise ValueError('排除须有明确范围证据；单页未提及、名单缺失不能证明不相关')
            elif self.dimension == 'role' and e.kind != 'job':
                raise ValueError('直接岗位关联须有招聘原文证据')
            elif self.dimension == 'city' and e.kind not in ('job', 'operating_unit'):
                raise ValueError('城市关联须有工作地点或实际经营机构证据')
        return self


class Assessments(Record):
    schema_version: Literal['company-assessments-1']
    role_context_hash: Text
    assessments: list[Assessment]


def role_context(config, expansion=None):
    expansion = RoleExpansion.model_validate(expansion) if expansion is not None else None
    inputs = {name_key(v) for v in config.role_keywords}
    groups, covered = [], set()
    if expansion and config.role_mode == 'keywords':
        explicit = {i.input_id: name_key(i.raw_text) for i in expansion.explicit_inputs}
        for g in expansion.ready_groups:
            keys = {name_key(t) for t in [g.primary_term, *g.aliases]}
            keys.update(explicit[i] for i in g.source_input_ids)
            matches = keys & inputs
            if matches:
                groups.append(g.model_dump())
                covered.update(matches)
    # Missing/legacy classifications cannot accidentally become specialist exclusions.
    for term in config.role_keywords if config.role_mode == 'keywords' else ['企业范围内不限岗位']:
        if name_key(term) in covered:
            continue
        groups.append({'group_id':'unclassified-' + digest(term)[:12], 'primary_term':term,
                       'aliases':[], 'nature':RoleNature().model_dump()})
    context = expansion.model_dump() if expansion else {'role_mode':config.role_mode, 'role_keywords':config.role_keywords,
                                                       'company_scope_keywords':config.company_scope_keywords}
    return groups, digest(context)


def merge_pool(catalog=None, knowledge=None, requested=()):
    pool = {}
    def add(record, origin):
        company = deepcopy(record)
        name = company.get('legal_name') or company.get('company_name')
        if not name or not name.strip():
            raise ValueError('企业选择输入缺少名称')
        key = name_key(name)
        observations = company.get('observations', [])
        discoveries = company.get('discoveries', [])
        company['legal_name'] = name
        company['source_url'] = company.get('source_url') or next((d['source_url'] for d in discoveries if d.get('source_url')), None)
        if key not in pool:
            pool[key] = company | {'selection_sources':[], 'observations':[], 'field_status':{}, 'user_requested':False}
        row = pool[key]
        row['selection_sources'].append({'kind':origin, 'source_url':company.get('source_url')})
        row['source_url'] = row.get('source_url') or company.get('source_url')
        row['observations'].extend(observations)
        for field, state in company.get('field_status', {}).items():
            if state == 'conflict' or row['field_status'].get(field) != 'conflict':
                row['field_status'][field] = state
    catalog = catalog or {}
    origin = catalog.get('source_kind', 'enterprise_directory')
    if origin not in ('enterprise_directory', 'listed_companies', 'knowledge_base'):
        raise ValueError('未知企业选择来源')
    for row in catalog.get('companies', []):
        add(row, origin)
    for row in knowledge or []:
        add(row, 'knowledge_base')
    for name in requested:
        if name_key(name) not in pool:
            add({'legal_name':name}, 'user_requested')
        pool[name_key(name)]['user_requested'] = True
    for row in pool.values():
        for field in ('company_intro', 'main_business', 'industry', 'operating_locations'):
            values = {json.dumps(o['value'],sort_keys=True,ensure_ascii=False) for o in row['observations']
                      if o.get('field') == field and o.get('status') == 'source_supported'}
            if len(values) > 1:
                if field == 'operating_locations':
                    try:
                        items = [item for o in row['observations'] if o.get('field')==field and o.get('status')=='source_supported'
                                 for item in (json.loads(o['value']) if isinstance(o['value'],str) else o['value'])]
                        if combine_locations(items) is not None:
                            continue
                    except (ValueError, TypeError, KeyError):
                        pass
                row['field_status'][field] = 'conflict'
    return list(pool.values())


def observed_facts(company, field):
    if company.get('field_status', {}).get(field) == 'conflict':
        return []
    facts = []
    for o in company.get('observations', []):
        if o.get('field') != field or o.get('status') != 'source_supported':
            continue
        e = o.get('evidence', {})
        if isinstance(e, str):
            try:
                e = json.loads(e)
            except ValueError:
                continue
        if not isinstance(e, dict):
            continue
        if name_key(e.get('entity_name', '')) != name_key(company['legal_name']):
            continue
        try:
            proof = SourceEvidence.model_validate({k:e[k] for k in ('entity_name','quote','content_hash','kind','subject_relation') if k in e} |
                                                 {'source_url':o['source_url'],'observed_at':o['observed_at']})
            if proof.subject_relation not in ('self', 'operating_unit'):
                continue
            value = json.loads(o['value']) if isinstance(o['value'], str) else o['value']
            value = normalize_value(field, value)
            facts.append((value, proof.model_dump()))
        except (ValueError, TypeError, KeyError):
            continue  # Legacy/incomplete observations remain visible but cannot justify promotion.
    return facts


def evaluate_company(company, city, groups, assessments):
    results = []
    for g in groups:
        states = {k:[] for k in ('role','business','city')}
        evidence, reasons = [], []
        terms = {name_key(t) for t in [g['primary_term'], *g['aliases']]}
        for records, proof in observed_facts(company, 'hiring_history'):
            if proof['kind'] != 'job' or proof['subject_relation'] != 'self':
                continue
            for record in records:
                if name_key(record.get('title','')) in terms:
                    states['role'].append('related')
                    evidence.append(proof)
                    reasons.append('已有同名岗位原文，仅证明值得查询，不证明当前在招')
                    if city in [normalize_city(c) for c in record.get('cities', [])]:
                        states['city'].append('related')
        for records, proof in observed_facts(company, 'operating_locations'):
            if proof['kind'] == 'operating_unit' and any(normalize_city(r['city']) == city for r in records):
                states['city'].append('related')
                evidence.append(proof)
                reasons.append('原文记录目标城市的实际经营机构，岗位地点仍须核验')
        for a in assessments:
            if (name_key(a.company_name), a.group_id, a.target_city) == (name_key(company['legal_name']),g['group_id'],city):
                states[a.dimension].append(a.verdict)
                reasons.append(a.rationale)
                evidence.extend(e.model_dump() for e in a.evidence)
        state = {k:next(iter(set(v))) if len(set(v)) == 1 else 'conflict' if v else 'unknown' for k,v in states.items()}
        conflicts = company.get('field_status', {})
        if any(conflicts.get(f) == 'conflict' for f in ('company_intro','main_business','industry')):
            state['business'] = 'conflict'
        if conflicts.get('operating_locations') == 'conflict':
            state['city'] = 'conflict'
        nature = g['nature']['kind']
        excluded = state['city'] == 'out_of_scope' or state['role'] == 'out_of_scope' or (
            nature == 'specialist' and state['business'] == 'out_of_scope' and state['role'] != 'related')
        if 'conflict' in state.values():
            excluded = False
        if state['role'] == 'related' and state['business'] == 'out_of_scope':
            reasons.append('直接岗位证据优先于业务排除判断，保留核查')
        if nature != 'specialist' and state['business'] == 'out_of_scope':
            reasons.append('通用、混合或不明确岗位不按行业/业务硬排除')
        rank = 0 if state['role'] == 'related' else 1 if state['business'] == 'related' and state['city'] == 'related' else 2 if nature == 'general' and state['city'] == 'related' else 3 if state['business'] == 'related' else 4
        known = state['city'] == 'related' and (state['role'] == 'related' or state['business'] == 'related' or nature == 'general')
        if nature in ('mixed', 'unclear') and state['role'] != 'related':
            known = False
        status = 'excluded' if excluded else 'eligible' if known and 'conflict' not in state.values() else 'review'
        if state['city'] == 'unknown':
            reasons.append('目标城市关联未知；异地总部/注册地及未提及城市均不能证明无岗位')
        if not known:
            reasons.append('资料不足或性质不明确，保留待核查，按预算查询')
        if 'conflict' in state.values():
            reasons.append('证据冲突，保留待核查')
        results.append({'group_id':g['group_id'], 'nature':nature, 'status':status, 'rank':rank,
                        'dimensions':state,'reasons':list(dict.fromkeys(reasons)), 'evidence':evidence})
    remaining = [r for r in results if r['status'] != 'excluded']
    best = min(remaining, key=lambda r:(r['rank'],r['group_id'])) if remaining else None
    requested = company.get('user_requested', False)
    return {'company':company['legal_name'], 'target_city':city, 'status':best['status'] if best else 'excluded',
            'rank':best['rank'] if best else 5, 'user_requested':requested, 'groups':results,
            'reasons':['用户明确指定，保留查询入口；不代表相关性或当前招聘已证实'] if requested else [],
            'sources':company['selection_sources']}


def select_companies(config, catalog=None, expansion=None, assessments=None, knowledge=None, requested=(), existing=()):
    groups, context_hash = role_context(config, expansion)
    batch = Assessments.model_validate(assessments) if assessments is not None else None
    if batch and batch.role_context_hash != context_hash:
        raise ValueError('企业判断与当前岗位词组/性质不同，请重新判断')
    if batch and any(a.group_id not in {g['group_id'] for g in groups} or a.target_city not in {normalize_city(c) for c in config.target_cities} for a in batch.assessments):
        raise ValueError('企业判断只能引用本次可用岗位组与目标城市，不可引用待确认组')
    pool = merge_pool(catalog, knowledge, requested)
    by_name = {name_key(c['legal_name']):c for c in pool}
    selected, decisions = {}, []
    for city in dict.fromkeys(normalize_city(c) for c in config.target_cities):
        old = {name_key(t['company']) for t in existing if t['target_city']==city and t.get('company')}
        available = None if config.budget.companies_per_city is None else max(0, config.budget.companies_per_city - len(old))
        ranked = [evaluate_company(c, city, groups, batch.assessments if batch else []) for c in pool]
        ranked.sort(key=lambda d:(not d['user_requested'],d['rank'],name_key(d['company'])))
        selected[city] = []
        for d in ranked:
            key = name_key(d['company'])
            company = by_name[key]
            if company.get('target_cities') and city not in [normalize_city(x) for x in company['target_cities']] and not d['user_requested']:
                d['selection'] = 'outside_discovery_scope'
                d['reasons'].append('不在本批企业线索指定的发现城市；未检查，不代表该城无业务或岗位')
            elif key in old:
                d['selection'] = 'already_planned'
            elif d['status'] == 'excluded' and not d['user_requested']:
                d['selection'] = 'excluded'
            elif available is None and not d['user_requested'] and d['status'] != 'eligible' and all(s['kind']=='knowledge_base' for s in d['sources']):
                d['selection'] = 'needs_scope_selection'
                d['reasons'].append('不限数量不自动遍历整库；请先选择本轮企业或补充岗位与城市关联证据')
            elif available is None or available > 0:
                d['selection'] = 'selected'
                if available is not None:
                    available -= 1
                selected[city].append(by_name[key] | {'selection_decision':d})
            else:
                d['selection'] = 'deferred_company_limit'
                if d['user_requested']:
                    selected[city].append(by_name[key] | {'selection_decision':d, 'company_budget_exhausted':True})
            decisions.append(d)
    return selected, {'schema_version':'company-selection-1','role_context_hash':context_hash,
                      'input_digest':digest({'catalog':catalog,'knowledge':knowledge,'requested':list(requested),
                                             'assessments':batch.model_dump() if batch else None}),
                      'semantic_validation':'assistant_authored_not_verified_by_python', 'decisions':decisions}
