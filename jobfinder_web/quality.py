"""Evidence tiers and source-bound job validation, shared by every web route."""
import re
from urllib.parse import urlsplit

TIERS={'search_snippet':'仅搜索摘要','onsite_list':'仅站内列表','original_detail':'原始页面内容'}


def source_fragment(text, quote):
    """Recover the literal source span, tolerating layout whitespace only."""
    if not isinstance(quote,str) or not quote.strip():return None
    if quote in text:return quote
    positions=[i for i,c in enumerate(text) if not c.isspace()]
    compact=''.join(text[i] for i in positions)
    wanted=''.join(c for c in quote if not c.isspace())
    start=compact.find(wanted)
    if start<0:return None
    return text[positions[start]:positions[start+len(wanted)-1]+1]


def specific_role(title, terms=()):
    # A campaign, fair or campus announcement is a discovery lead, not a vacancy.
    if any(t.casefold() in title.casefold() for t in terms) and not re.search('汇总|公告|招聘会|招聘计划',title):return True
    return not bool(re.search(r'(?:\d{2,4}届.*(?:校招|招聘)|校园招聘(?:公告|简章)?|招聘简章|招聘公告|招聘会|宣讲会|招聘计划|人才招募计划|热招职位汇总)',title))


def vacancy_identity(url):
    path=urlsplit(url).path
    found=re.search(r'/((?:CCL?\d+)J\d+)\.(?:htm|html)$|/job_detail/([^/.]+)\.html$|/job/(\d+)\.shtml$',path,re.I)
    return next((g for g in found.groups() if g),None) if found else None


def role_admission(fields, terms, adjacent=(), relation=None, quote=None):
    """Admission is separate from matching. Requirements alone prove no duties."""
    fold=lambda s:re.sub(r'\s+','',s or '').casefold()
    title=fold(fields.get('title'))
    if any(fold(t) in title for t in terms if fold(t)):
        return {'eligible':True,'basis':'岗位名称命中目标岗位词','kind':'direct_title'}
    duties=fields.get('description') or ''
    span=source_fragment(duties,quote)
    finance_bp=any(fold(t) in ('财务bp','财务业务伙伴','financebusinesspartner','financialbusinesspartner') for t in terms)
    if finance_bp and re.search(r'hrbp|人力资源|人事|招聘专员',title):
        return {'eligible':False,'basis':'人力资源职能不能因 BP 或财务相关专业要求进入财务岗位表','kind':'different_function'}
    supported=relation=='related' and span and len(fold(span))>=12 and len(fold(duties))>=20
    if supported and finance_bp:
        # A literal quote plus a model claim is insufficient when the quote is
        # just bookkeeping or qualifications. Require financial planning/analysis.
        supported=bool(re.search(r'财务|预算|成本|利润|营收|finance|financial|budget',span,re.I) and
                       re.search(r'分析|经营|业务.{0,12}(?:支持|伙伴)|决策|预算.{0,12}(?:编制|管理|执行)|analysis|planning|business',span,re.I))
    if supported:
        return {'eligible':True,'basis':'目标职能判断有本岗位职责原文支持','kind':'supported_duties','quote':span}
    return {'eligible':False,'basis':'相邻或其他岗位缺少可核验的目标职能职责，保留线索待补详情','kind':'duties_required'}


def lead_relevant(fields, terms):
    # Compatibility helper: without an explicit source-bound duty assessment,
    # only a directly related title is enough to retain a partial lead.
    return role_admission(fields,terms)['eligible']


def location_conflicts(title, description, location):
    if not location:return {}
    # Only explicit actual workplace statements, not incidental city mentions.
    explicit=re.findall(r'(?:base\s*(?:在|于|[:：/])?\s*|实际工作地(?:点)?\s*[:：为是]?\s*)([\u4e00-\u9fff]{2,8})',title+' '+description,re.I)
    from jobfinder.geography import normalize_city
    def city(value):
        from jobfinder.geography import evaluate_location
        from jobfinder.models import Job
        j=Job(company='检查',title='检查',description='',source_url='https://example.com',source_channel='unknown',location_raw=value)
        return evaluate_location(j,value,[]).city
    listed=city(location)
    different=[x for x in explicit if city(x) and listed and normalize_city(city(x))!=normalize_city(listed)]
    return {'location_raw':list(dict.fromkeys([location]+different))} if different else {}


def page_evidence_tier(page):
    """A readable search/list page is still a list, irrespective of its reader."""
    tier=page.get('evidence_level','original_detail')
    if tier!='original_detail':return tier
    from .page_signals import is_job_detail_url
    path=urlsplit(page.get('url','')).path
    if vacancy_identity(page.get('url','')) or is_job_detail_url(page.get('url','')):return tier
    # Observed list route shapes, not company identity or an invented detail URL.
    listing_path=re.search(r'/(?:search|sou|zhaopin|city-[^/]+)(?:/|$)|/(?:jobs|positions)/?$',path,re.I)
    listing_text=re.search(r'共(?:找到)?\s*\d+\s*(?:个|条)?\s*(?:相关)?(?:职位|岗位)|职位列表|岗位列表|职位搜索',page.get('text',''))
    detail_links={x.get('url') for x in page.get('links',[]) if is_job_detail_url(x.get('url',''))}
    if listing_path or (listing_text and len(detail_links)>1):return 'onsite_list'
    return tier


def bound_detail_link(page, block, fields):
    """Resolve only a link observed inside this company's own vacancy record.

    Page-wide title equality is not identity: two employers can advertise the
    same role. Plain browser links lacking card context remain unbound.
    """
    fold=lambda value:re.sub(r'\s+','',value or '').casefold()
    record,title,company=map(fold,(block,fields['title'],fields['company']))
    targets=set()
    for link in page.get('links',[]):
        if not vacancy_identity(link.get('url','')):continue
        label=fold(link.get('text',''))
        # An anchor wrapping the complete card carries its own identity.
        anchored=bool(title and company and label.count(title)==1 and company in label and label in record)
        context=fold(link.get('record_context'))
        # HTML context is the immediate element only, not a whole-page title
        # search. Do not bind records containing another occurrence of the role.
        local=bool(label==title and record.count(title)==1 and context and company in context and
                   context in (record,record+label,label+record))
        if anchored or local:targets.add(link['url'])
    return next(iter(targets)) if len(targets)==1 else None


def _experience_minimum(value):
    """Only for finding contradictory source statements, never final matching."""
    if re.search(r'不限|无需|无经验',value):return 0
    number=re.search(r'[0-9]+(?:\.[0-9]+)?|[一二三四五六七八九十两]+',value)
    if not number:return None
    text=number[0]
    if text[0].isdigit():return float(text)
    digits=dict(zip('一二三四五六七八九两',range(1,10)));digits['两']=2
    if '十' in text:
        a,b=text.split('十',1)
        return digits.get(a,1)*10+digits.get(b,0)
    return digits.get(text)


def validate_field_semantics(fields, quotes, block, body):
    """Require field-specific evidence in the same record; retain conflicts.

    Literal substring checks alone admit the word 不限 from education or a date
    filter. Never use the user's inputs or another record to repair a field.
    """
    notes=[];conflicts={}
    raw=fields.get('experience_raw')
    statements=[]
    number=r'(?:\d+(?:\.\d+)?|[一二三四五六七八九十两]+)'
    years=number+r'\s*(?:[-—–~～至]\s*'+number+r')?\s*年(?:及以上|以上|以下|以内)?'
    explicit=re.findall(r'(?:工作)?经验\s*[:：]?\s*不限|不限(?:工作)?经验|无需(?:工作)?经验|无(?:工作)?经验(?:要求)?',block)
    statements.extend(explicit)
    # "3年" is not sufficient on its own: it may describe a company age,
    # contract length or applicant age. Require an experience label/sentence.
    for pattern in [r'(?:(?:工作)?经验(?:要求|年限)?|工作年限)\s*[:：]?\s*'+years,
                    years+r'[^。；;，,\n：:\d]{0,25}经验(?:者)?(?:优先|为佳)?']:
        for match in re.finditer(pattern,block):
            before=block[max(0,match.start()-8):match.start()]
            if not re.search(r'(?:成立|创办|创立|年龄|年纪|合同|聘期)\s*[:：]?\s*$',before):
                statements.append(match[0])
    # Unlabelled list metadata has an observable city → experience → education
    # order. Use this role's actual fields, not a page-wide occurrence of 不限.
    location,education=fields.get('location_raw'),fields.get('education_raw')
    if location and education:
        exp=r'(?:不限|'+years+r')'
        for pattern in [re.escape(location)+r'\s*('+exp+r')\s*'+re.escape(education),
                        re.escape(location)+r'\s*'+re.escape(education)+r'\s*('+exp+r')']:
            match=re.search(pattern,block)
            if match:statements.append(match[1])
    statements=list(dict.fromkeys(statements))
    requires_context=bool(raw and (re.search(years,raw) or re.search(r'不限|无(?:工作)?经验|无需(?:工作)?经验',raw)))
    if requires_context:
        supporting=[s for s in statements if source_fragment(s,raw) or source_fragment(raw,s)]
        if not supporting:
            quotes['experience_unverified']=raw
            fields.pop('experience_raw',None);quotes.pop('experience_raw',None)
            notes.append('经验字段未找到本岗位的经验语义证据，保留为未披露；年数、学历或筛选栏的“不限”不能代替工作经验')
            raw=None
        elif re.search(years,raw):
            # Keep qualifiers when the model copied only the numeric substring.
            # An exact metadata value wins; otherwise use the full source span.
            supported=next((s for s in supporting if s==raw),supporting[0])
            if re.search(r'以下|以内|优先|为佳',supported) and supported!=raw:
                raw=fields['experience_raw']=supported;quotes['experience_raw']=supported
    # If the model omitted or selected an unsupported value, recover only an
    # unambiguous explicitly labelled / ordered original statement.
    if not raw and statements and len({_experience_minimum(x) for x in statements})==1:
        fields['experience_raw']=statements[0];quotes['experience_raw']=statements[0]
    hard_statements=[s for s in statements if not re.search(r'优先|为佳',s)]
    if len({x for s in hard_statements if (x:=_experience_minimum(s)) is not None})>1:
        conflicts['experience_raw']=hard_statements
        for i,statement in enumerate(statements,1):quotes[f'experience_raw_alternative_{i}']=statement
        notes.append('同一岗位的经验年限存在不同原文要求，保留冲突待核实')
    location=fields.get('location_raw')
    if location:
        explicit_work=re.search(r'(?:工作地(?:点)?|工作城市|任职地点|办公地址|base)\s*[:：为]?\s*'+re.escape(location),block,re.I)
        venue=re.search(r'(?<!工作)(?<!任职)地点\s*[:：]\s*'+re.escape(location),block)
        event_context=bool(venue and re.search(r'招聘会|宣讲会|双选会|时间\s*[:：]',block[max(0,venue.start()-180):venue.start()]))
        if event_context and not explicit_work:
            fields.pop('location_raw',None);quotes.pop('location_raw',None)
            quotes['recruitment_venue']=location
            notes.append('招聘活动场地不作为岗位工作地点：'+location+'；实际工作地点待核实')
    return notes,conflicts


def complete_detail(fields, block, tier, body):
    if tier!='original_detail':return False
    if re.search(r'登录.{0,12}(?:查看|解锁).{0,12}(?:完整|全部)|查看完整.{0,8}(?:登录|会员)|展开全部|登录后可见',body):return False
    duties=fields.get('description',''); requirements=fields.get('requirements','')
    if re.search(r'\.{3,}|…|查看(?:更多|全部|完整)|展开(?:更多|全文|全部)',block+' '+duties+' '+requirements):return False
    # Both must carry substantive, distinct role text, not just metadata like 本科.
    return (len(duties)>=35 and len(requirements)>=25 and duties!=requirements
            and bool(re.search(r'负责|参与|支持|完成|分析|管理|develop|responsib|support',duties,re.I))
            and bool(re.search(r'学历|经验|熟悉|能力|专业|要求|资格|experience|degree|skill',requirements,re.I)))


def merge_jobs(jobs):
    """Upgrade the same observed vacancy; never merge two roles on a collection URL."""
    from jobfinder.matching import deduplicate, identity_text, same_source_id
    from jobfinder.geography import normalize_city, same_city_evidence
    priority={'search_snippet':0,'onsite_list':1,'original_detail':2,None:2}

    def same_vacancy(a, b):
        if a.source_channel != b.source_channel:
            return False
        if a.source_id and b.source_id and not same_source_id(a,b):
            return False
        same_id = same_source_id(a,b)
        same_detail = (a.source_url == b.source_url and bool(vacancy_identity(a.source_url)))
        if not same_id and a.source_url != b.source_url:
            return False
        if any(identity_text(getattr(a,k)) != identity_text(getattr(b,k)) for k in ('company','title','level')):
            return False
        if not same_id and not same_detail:
            # Collection pages can advertise the same title in different cities.
            # Even two districts in the same city can be separate vacancies.
            if a.detail_url and b.detail_url and a.detail_url != b.detail_url:
                return False
            if a.location_raw and b.location_raw and identity_text(a.location_raw) != identity_text(b.location_raw):
                return False
            ca, cb = normalize_city(a.location_raw or ''), normalize_city(b.location_raw or '')
            if ca and cb and ca != cb:
                return False
            if a.locations and b.locations and a.locations != b.locations and (a.location_mode != 'single' or b.location_mode != 'single'):
                return False
        return True

    out=[]
    for original in jobs:
        job=original.model_copy(deep=True)
        old=next((j for j in out if same_vacancy(j,job)),None)
        if old is None:out.append(job);continue
        rank=lambda j:(priority[j.evidence_level],bool(j.details_complete),len(j.description or ''))
        winner,other=(job,old) if rank(job)>rank(old) else (old,job)
        if winner is job:
            out[out.index(old)]=winner
        winner.evidence.extend(e for e in other.evidence if e not in winner.evidence)
        winner.notes=list(dict.fromkeys(winner.notes+other.notes))
        winner.discovery_task_ids=list(dict.fromkeys(winner.discovery_task_ids+other.discovery_task_ids))
        winner.discovery_cities=list(dict.fromkeys(winner.discovery_cities+other.discovery_cities))
        winner.validity_checked_at=max(filter(None,[winner.validity_checked_at,other.validity_checked_at]),default=None)
        for field,values in other.conflicts.items():
            winner.conflicts[field]=list(dict.fromkeys(winner.conflicts.get(field,[])+values))
        for field in ('location_raw','education_raw','salary_raw','experience_raw','posted_at','deadline','open_status','application_url'):
            a,b=getattr(winner,field),getattr(other,field)
            if (not a or field=='open_status' and a=='UNKNOWN') and b:
                setattr(winner,field,b)
            elif a and b and identity_text(a)!=identity_text(b):
                if field=='open_status' and b=='UNKNOWN':continue
                if field!='location_raw' or not same_city_evidence([a,b]):
                    winner.conflicts[field]=list(dict.fromkeys(winner.conflicts.get(field,[])+[a,b]))
                    if field=='open_status':winner.open_status='UNKNOWN'
        places=winner.conflicts.get('location_raw',[])
        if places and same_city_evidence(([winner.location_raw] if winner.location_raw else [])+places):
            winner.conflicts.pop('location_raw')
        if {'OPEN','CLOSED'}.issubset(winner.conflicts.get('open_status',[])):
            winner.open_status='UNKNOWN'
        if priority[job.evidence_level]==priority[old.evidence_level] and job.description and old.description and job.description not in old.description and old.description not in job.description:
            winner.notes=list(dict.fromkeys(winner.notes+['同一岗位有不同正文片段，完整性待核实']))
            winner.details_complete=False
        # Revalidate derived facts after filling a raw field from another witness.
        out[out.index(winner)]=type(winner).model_validate(winner.model_dump(mode='json'))
    return deduplicate(out)
