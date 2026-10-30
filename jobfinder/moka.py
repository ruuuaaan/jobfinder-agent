"""Parse original Moka HTML bootstrap JSON, never execute scripts or use tokens."""
import json
import hashlib
import re
from pathlib import Path
from .models import Job, Evidence


def bootstrap(doc):
    raw=doc.xpath('//input[@id="init-data"]/@value')
    if not raw:
        return None
    try:
        obj=json.loads(raw[0])
    except (ValueError,TypeError):
        return None
    if not isinstance(obj,dict) or not isinstance(obj.get('org'),dict) or not isinstance(obj.get('jobs'),list):
        return None
    if not obj['org'].get('id') or not obj['org'].get('name') or obj.get('mode') not in ('social','campus',None):
        return None
    return obj


def parse_bootstrap(doc,data,candidate,snapshot,retrieved_at):
    from .parsers import clean,enrich
    obj=bootstrap(doc)
    if obj is None:
        return None
    jobs=[]
    for row in obj['jobs']:
        if not isinstance(row,dict) or row.get('orgId')!=obj['org']['id'] or not row.get('id') or not row.get('title'):
            continue
        locs=row.get('locations') or ([row['location']] if row.get('location') else [])
        places=[' '.join(str(x.get(k) or '') for k in ('country','province','city','address')).strip() for x in locs if isinstance(x,dict)]
        codes=[str(x['cityId']) for x in locs if isinstance(x,dict) and x.get('cityId') is not None]
        description=clean(row.get('description') or row.get('detail')) or ''
        requirements=clean(row.get('requirements'))
        j=Job(company=obj['org']['name'],title=row['title'],description=description,requirements=requirements,
              source_url=candidate.url,source_channel=candidate.source_channel,source_id=str(row['id']),
              location_raw='；'.join(places) or None,locations=places,location_codes_raw=codes,
              location_mode='unknown' if len(places)>1 else 'single',
              education_raw=row.get('education') if isinstance(row.get('education'),str) else None,
              experience_raw=row.get('experience') if isinstance(row.get('experience'),str) else None,
              posted_at=row.get('publishedAt'),open_status={'open':'OPEN','closed':'CLOSED'}.get(row.get('status'),'UNKNOWN'),
              details_complete=bool(description and requirements),synthetic=candidate.synthetic,
              notes=['公司名称为原站招聘品牌，实际签约法人主体待确认；未把候选企业名称代填',
                     '来源为原页init-data中的逐岗位记录，不是搜索摘要；投递入口未核验，留空'])
        if codes:
            j.notes.append('原始cityId='+','.join(codes)+'；未验证该来源代码定义前，不凭数字猜测城市')
        if not j.details_complete:
            j.notes.append('岗位详情未完整取得；职责、学历、经验等缺失字段不从列表筛选选项补齐')
        if len(places)>1:
            j.notes.append('多个地点的独立选择或轮岗关系未核实')
        j=enrich(j,data,'moka_html_bootstrap',snapshot,retrieved_at)
        # Keep only job-field evidence; ignore rewards, hidden auth/config values.
        j.evidence[-1].field_quotes.update(source_id=str(row['id']),open_status=str(row.get('status','')),location_codes_raw=','.join(codes))
        jobs.append(j)
    return jobs


def attach_city_evidence(jobs,doc,contract):
    """Map only codes whose meaning is proved by the referenced client bundle."""
    from .geography import BY_CODE,normalize_city
    if not contract or contract.get('evidence_url') not in doc.xpath('//script/@src'):
        return
    from .paths import WORKSPACE as root
    path=Path(contract.get('snapshot','')).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return
    data=path.read_bytes()
    if hashlib.sha256(data).hexdigest()!=contract.get('content_hash'):
        return
    script=data.decode('utf-8',errors='replace')
    if 'getCityNameFromLocation' not in script or 'cityNamesOnId' not in script:
        return
    for j in jobs:
        if len(j.locations)!=len(j.location_codes_raw):
            continue
        for raw,code in zip(j.locations,j.location_codes_raw):
            if not re.fullmatch(r'\d{6}',code) or not raw.startswith(('中国','China')) or code[:2] in ('71','81','82'):
                continue
            name=BY_CODE.get(code)
            parent=BY_CODE.get(code[:4]+'00') or BY_CODE.get(code[:2]+'0000')
            quote=re.search(r'(?<!\d)'+code+r':"([^"\n]+)"',script)
            if not name or not quote or quote[1]!=name:
                continue
            city=normalize_city(name) or normalize_city(parent or '')
            if city:
                j.evidence.append(Evidence(source_url=contract['evidence_url'],retrieved_at=contract['retrieved_at'],content_hash=contract['content_hash'],snapshot=str(path),method='moka_city_code',field_quotes={'location_raw':raw,'code':code,'city':city,'code_quote':quote[0],'parent_from_registry':parent or ''}))
                j.notes=[x for x in j.notes if not x.startswith('原始cityId=')]
                j.notes.append('岗位地点代码经招聘页引用脚本与本地行政区划表交叉核验；原文地址和代码单独保留')


def read_details(fetcher,jobs,obj,entry,targets,reports,base_report,limit=4):
    from urllib.parse import urlsplit
    from .network import FetchError
    from .geography import evaluate_location,belongs,normalize_city
    from .parsers import clean,enrich
    origin=urlsplit(entry)
    contract=fetcher.policies.get(origin.hostname,{}).get('moka_public_job_read')
    if not contract:
        return
    used=0
    for j in jobs:
        coded={e.field_quotes['city'] for e in j.evidence if e.method=='moka_city_code'}
        if coded and not any(belongs(c,normalize_city(t)) for c in coded for t in targets):
            continue
        if evaluate_location(j,j.location_raw,targets).state=='FAIL':
            continue
        if used>=limit:
            j.notes.append('本次详情请求小样本上限已到，详情尚未检查')
            continue
        used+=1
        r={k:v for k,v in base_report.items() if k not in ('discovered_links','adapter')}
        r.pop('content_read_at',None)
        r.pop('validity_checked_at',None)
        r.update(url=contract['url'],source_id=j.source_id,parent_url=entry,access_status='not_run',parse_status='not_run',jobs_extracted=None,reason='',attempted=True,candidate_type='job_detail')
        reports.append(r)
        try:
            page=fetcher.read_moka_job(contract['url'],dict(orgId=obj['org']['id'],siteId=obj['siteId'],jobId=j.source_id,locale='zh-CN'))
            r.update(access_status='success',content_read_at=page['retrieved_at'])
            response=json.loads(page['data'])
            if isinstance(response,dict) and isinstance(response.get('data'),str):
                r.update(parse_status='encoded_payload',reason='返回编码响应而非可核验的岗位JSON；本适配器不解码，需官方页面查看，不是已确认需要登录')
                j.notes.append('岗位详情接口返回编码内容，正文尚未核验；可在官方页面人工查看')
                break
            row=response.get('data',response) if isinstance(response,dict) else None
            if not isinstance(row,dict) or str(row.get('id'))!=j.source_id or row.get('orgId')!=obj['org']['id']:
                raise ValueError('详情响应与原始岗位ID/招聘组织不一致或响应结构尚未验证')
            if row.get('title') and row['title']!=j.title:
                raise ValueError('列表与详情职位名冲突，需复核')
            desc=clean(row.get('jobDescription'))
            if not desc:
                raise ValueError('详情响应没有已支持的jobDescription正文')
            j.description=desc
            parts=re.split(r'任职要求[：:]?|任职资格[：:]?|岗位要求[：:]?',desc,maxsplit=1)
            j.requirements=parts[1].strip() if len(parts)>1 else None
            j.education_raw=row.get('education') if isinstance(row.get('education'),str) else None
            if j.education_raw=='不限': j.education_raw='学历不限'
            j.experience_raw=row.get('experience') if isinstance(row.get('experience'),str) else None
            if row.get('status') in ('open','closed'):
                j.open_status={'open':'OPEN','closed':'CLOSED'}[row['status']]
            j.details_complete=True
            j.validity_checked_at=page['retrieved_at']
            j.notes=[x for x in j.notes if not x.startswith(('岗位详情未完整取得','来源为原页init-data'))]
            j.notes.append('原始公开只读接口已读取详情；未执行投递，投递链接仍留空')
            enrich(j,page['data'],'moka_public_job_detail',page['snapshot'],page['retrieved_at'])
            j.evidence[-1].source_url=contract['url']
            j.evidence[-1].field_quotes.update(source_id=j.source_id,request=json.dumps(page['request'],ensure_ascii=False))
            r.update(parse_status='success',jobs_extracted=0,reason='补全已提取岗位的详情；不是新增岗位，数量不重复计算')
        except FetchError as exc:
            r.update(access_status=exc.status,reason=str(exc))
            j.notes.append('详情读取未完成：'+exc.status)
            if exc.status in ('blocked','requires_authorization','rate_limited','tls_error','budget_exhausted'):
                break
        except (ValueError,TypeError) as exc:
            r.update(parse_status='failed',reason=str(exc))
