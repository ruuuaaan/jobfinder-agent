"""Process live, visible-DOM receipts through the normal JobFinder core.

Browser execution lives in scripts/browser_collector.mjs, injected into Codex's
documented browser runtime. This module performs no network requests.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .models import Evidence, Job, now
from .matching import deduplicate, evaluate
from .exporter import export_bundle
from .planning import effective_config, fingerprint


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def section(text, starts, ends):
    prefix = r'(?:[（(][一二三四五六七八九十]+[）)]|\d+[.、])?\s*'
    start = re.search(r'(?:^|\n)\s*'+prefix+r'(?:'+starts+r')\s*[:：]?\s*\n', text)
    if not start:
        return ''
    rest = text[start.end():]
    end = re.search(r'(?:^|\n)\s*'+prefix+r'(?:'+ends+r')', rest)
    return rest[:end.start()].strip() if end else rest.strip()


def parse_detail(detail, snapshots, company_spec):
    page = snapshots[detail['snapshot_id']]
    listing = snapshots[detail['card']['snapshot_id']]
    fields, text, card = page['fields'], page['text'], detail['card']
    title = fields.get('title') or card['title']
    # The selected title must be in the primary header, not only in recommendations.
    header = re.split(r'\n\s*(?:岗位职责|工作职责|职位描述)\s*\n', text, maxsplit=1)[0]
    # Some detail headers append a category badge to the title container, while
    # the same primary header also renders the exact job title on its own line.
    if title != card['title'] and title.startswith(card['title']) and card['title'] in [line.strip() for line in header.splitlines()]:
        title = card['title']
    if title != card['title'] or title not in header:
        raise ValueError('Detail title does not match selected list record: '+card['title'])
    # Exclude global navigation (e.g. “博士后招聘”) from education/date extraction.
    header=header[header.index(title):]
    description = fields.get('description') or section(text, '岗位职责|工作职责|职位描述', '岗位要求|任职要求|任职资格|岗位基本要求|职位要求|申请岗位|立即投递|沪公网安备')
    # Some sites wrap numbered duties and requirements inside an outer 任职要求.
    # Anchor the requirement section after the extracted duties, avoiding that wrapper.
    requirement_text = text[text.index(description)+len(description):] if description else text
    requirements = fields.get('requirements') or section(requirement_text, '岗位要求|任职要求|任职资格|岗位基本要求|职位要求', '具备以下条件优先|岗位亮点|相关推荐岗位|报名要求|其他事项|申请岗位|立即投递|沪公网安备|©|Copyright')
    company = card.get('company') or detail['company']
    location = fields.get('location') or card.get('location') or ''
    # Literal source fragments only; missing data is kept missing.
    education = re.search(r'(?:博士|硕士|本科|大专|中专|高中)(?:及以上|以上|及以下|以下)?(?:学历)?', requirements+'\n'+header)
    experience = next((line.strip() for line in requirements.splitlines() if re.search(r'(?:\d|[一二三四五六七八九十])[~～—\-\d]*年.{0,35}(?:经验|经历)|经验不限|无需经验', line)), None)
    if experience:
        experience=re.sub(r'^\d+[.、．]\s*','',experience)
    salary = re.search(r'面议|\d+(?:\.\d+)?\s*[-~～]\s*\d+(?:\.\d+)?\s*[kK万千元](?:/月|/年|·\d+薪)?', header)
    date = re.search(r'(20\d{2})[年/-](\d{1,2})[月/-](\d{1,2})', header)
    posted = '-'.join([date[1],date[2].zfill(2),date[3].zfill(2)]) if date else None
    params=parse_qs(urlsplit(page['url']).query)
    source_id=next((params[key][0] for key in ('postId','jobAdId','jobUnionId') if params.get(key)),None)
    apply = re.search(r'申请岗位|立即投递|立即申请|我要报名|申请职位', text)
    quotes = {'title':title,'description':description,'requirements':requirements}
    for key,value in [('company',company),('location_raw',location),('education_raw',education[0] if education else None),('experience_raw',experience),('salary_raw',salary[0] if salary else None)]:
        if value and value in text:
            quotes[key]=value
    for value in quotes.values():
        if value and value not in text:
            raise ValueError('Extracted value is not a literal visible-DOM quote')
    if not company or (company not in text and company not in listing['text']):
        raise ValueError('Company name has no visible source evidence')
    complete = bool(len(description)>20 and len(requirements)>20)
    evidence = [Evidence(source_url=page['url'],retrieved_at=page['retrieved_at'],content_hash=page['content_hash'],
                         method='browser_visible_dom',snapshot='browser-run.json#'+page['id'],field_quotes=quotes)]
    evidence.append(Evidence(source_url=listing['url'],retrieved_at=listing['retrieved_at'],content_hash=listing['content_hash'],
                            method='browser_visible_dom_list',snapshot='browser-run.json#'+listing['id'],
                            field_quotes={k:v for k,v in {'company':company,'title':title,'location_raw':location}.items() if v and v in listing['text']}))
    notes=['官网未明确标注当前开放状态；投递按钮未点击，未自动投递。']
    missing = [key for key,value in [('薪资',salary),('学历',education),('经验',experience),('工作地点',location)] if not value]
    if missing:
        notes.append('原文未明确披露：'+'、'.join(missing))
    return Job(company=company,title=title,description=description,requirements=requirements or None,
               source_url=page['url'],detail_url=page['url'],source_channel='official_career',source_id=source_id,
               source_type='official_website' if company_spec.get('source_type')=='official_website' else 'recruitment_provider',
               location_raw=location or None,locations=[location] if location else [],education_raw=education[0] if education else None,
               experience_raw=experience,salary_raw=salary[0] if salary else None,recruitment_type=detail.get('recruitment_type'),
               posted_at=posted,validity_checked_at=page['retrieved_at'],open_status='UNKNOWN',details_complete=complete,evidence=evidence,notes=notes,
               application_url=page['url'] if apply else None,application_method='在原岗位页点击“'+apply[0]+'”；可能要求登录' if apply else None)


def apply_role_reviews(jobs, reviews, config, snapshots):
    for review in reviews:
        targets=[j for j in jobs if j.source_url==review.get('source_url')]
        if len(targets)!=1 or review.get('role_keywords')!=config.role_keywords:
            raise ValueError('Role review URL or original role context mismatch')
        job=targets[0]
        if review.get('decision') not in ('related','out_of_scope','unknown'):
            raise ValueError('Unsupported role review decision')
        proof=next((p for p in snapshots.values() if p['url']==job.source_url and p['content_hash']==review.get('content_hash') and review.get('quote') and review['quote'] in p['text']),None)
        if not proof or not review.get('rationale'):
            raise ValueError('Role review requires original quote and matching snapshot hash')
        job.role_review={k:review[k] for k in ('role_keywords','decision','quote','content_hash','rationale')}


def process_browser_run(config, input_path, output, resource_path=None, mode='real', role_reviews_path=None):
    input_path,output=Path(input_path),Path(output)
    receipt=json.loads(input_path.read_text(encoding='utf-8-sig'))
    if receipt.get('schema_version')!='browser-run-1' or receipt.get('runtime')!='codex_browser_visible_dom':
        raise ValueError('Unsupported browser receipt; manual imports use the existing import path')
    if receipt.get('mode')!=mode:
        raise ValueError('Browser receipt mode mismatch')
    if receipt.get('plan',{}).get('config_fingerprint')!=fingerprint(config):
        raise ValueError('Browser plan config fingerprint mismatch')
    snapshots={}
    for p in receipt['snapshots']:
        if p['id'] in snapshots or p['content_hash']!=digest(p['text']):
            raise ValueError('Duplicate snapshot id or evidence hash mismatch')
        if not re.match(r'^https?://',p['url']) or not p.get('retrieved_at'):
            raise ValueError('Missing original source URL or retrieval time')
        snapshots[p['id']]=p
    specs={s['company']:s for s in receipt['plan']['companies']}
    jobs=[]
    errors=[]
    for d in receipt['details']:
        try:
            job=parse_detail(d,snapshots,specs[d['company']])
            job.discovery_cities=list(config.target_cities)
            job.synthetic=mode=='test'
            jobs.append(job)
        except (ValueError, KeyError) as exc:
            errors.append({'detail_id':d.get('id'),'company':d.get('company'),'reason':str(exc)})
    jobs=deduplicate(jobs)
    reviews=json.loads(Path(role_reviews_path).read_text(encoding='utf-8-sig')) if role_reviews_path else []
    apply_role_reviews(jobs,reviews,config,snapshots)
    results=[r for j in jobs for r in evaluate(j,config)]
    reports=[]
    companies=[]
    for c in receipt['companies']:
        own=[j for j in jobs if any(snapshots[e.snapshot.split('#')[-1]]['company']==c['company'] for e in j.evidence)]
        failed=[e for e in errors if e['company']==c['company']]
        complete=c.get('coverage_complete') is True and not failed
        reports.append(dict(company=c['company'],channel='official_career',url=c['entry_url'],attempted=True,target_cities=config.target_cities,
                            access_status='success' if c['pages'] else 'failed',parse_status='success' if complete else 'partial',
                            jobs_extracted=len(own),reason='；'.join(c['problems']+[e['reason'] for e in failed]) or '声明范围读取完成；是否匹配按原始条件判断',
                            browser_status=c['status'],coverage_complete=complete,listed_count=len(c['listed']),reported_total=c.get('reported_total'),
                            detail_count=len(own),scope=c['scope'],full_detail_count=sum(j.details_complete for j in own)))
        companies.append({'legal_name':c['company'],'career_url':c['entry_url'],'verification_status':'browser_'+c['status'],'notes':c['scope']})
    discovery={'provider':'codex_browser_visible_dom','status':'complete' if all(r['coverage_complete'] for r in reports) else 'partial',
               'operation':'browser_collection','coverage':'；'.join(c['company']+'：'+c['scope'] for c in receipt['companies']),
               'candidates':[{'company':c['company'],'url':c['entry_url'],'candidate_type':'career_entry','target_cities':config.target_cities} for c in receipt['companies']],
               'search_log':[],'effective_config':effective_config(config,None,{}),
               'browser_action_count':len(receipt['events']),'browser_snapshot_count':len(snapshots),'core_http_requests':0,
               'browser_actions_scope':'自动采集脚本阶段，含重试；不含前期页面结构观察',
               'browser_receipt':str(input_path.resolve()),'parse_errors':errors,'role_reviews':reviews}
    payload=export_bundle(output,config,jobs,results,reports,companies,discovery,events=[],mode=mode,annotate_reviews=True)
    (output/'browser-run.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
    write_browser_audit(output,config,input_path,receipt,payload,role_reviews_path)
    if resource_path and mode=='real':
        from .resources import ResourceStore
        store=ResourceStore(Path(resource_path))
        for c,r in zip(receipt['companies'],reports):
            pages=[p for p in receipt['snapshots'] if p['company']==c['company']]
            if not pages:
                continue
            p=pages[-1]
            store.add(c['company'],p['url'],p['retrieved_at'],'entry_check',context={'entry':{
                'url':c['entry_url'],'entry_type':'career_entry','source_type':specs[c['company']].get('source_type','recruitment_provider'),
                'source_channel':'official_career','source_url':p['url'],'checked_at':p['retrieved_at'],
                'availability':'available' if c['pages'] else 'temporary_failure','reason':r['reason'],
                'evidence':{'quote':p['text'][:1000],'content_hash':p['content_hash'],'method':'browser_visible_dom'},
                'recommended_read_method':'browser_visible_dom'}})
        for job in jobs:
            ev=job.evidence[0]
            if ev.field_quotes.get('company')==job.company:
                store.add(job.company,job.source_url,ev.retrieved_at,'job_original',
                          values={'hiring_history':[{'title':job.title,'cities':list(dict.fromkeys(r.geo.city for r in evaluate(job,config) if r.geo.city))}]},
                          supported=('hiring_history',),evidence={'quote':json.dumps(ev.field_quotes,ensure_ascii=False),'content_hash':ev.content_hash,
                          'entity_name':job.company,'kind':'job','subject_relation':'self'})
        store.snapshot()
    write_report(output,payload,receipt)
    return payload


def write_browser_audit(output,config,input_path,receipt,payload,role_reviews_path=None):
    artifacts={}
    for name,path in [('browser_receipt',input_path),('role_reviews',role_reviews_path)]:
        if path:
            path=Path(path)
            artifacts[name]={'path':str(path.resolve()),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    audit=dict(schema_version='core-execution-1',recorded_at=now(),core_invoked=True,operation='browser_collection',
               config_fingerprint=fingerprint(config),input_artifacts=artifacts,read_attempts=0,http_requests=0,
               successful_content_http_requests=0,robots_http_requests=0,native_checkpoint=None,
               browser_action_count=len(receipt['events']),browser_snapshot_count=len(receipt['snapshots']),
               browser_internal_http_requests=None,jobs_extracted=len(payload['jobs']),counts=payload['counts'],
               pipeline=['parse_browser_evidence','deduplicate','evaluate','export_bundle'],
               notes=['Python处理浏览器回执，不执行HTTP；浏览器内部网络请求数量未知。','浏览器动作仅统计脚本采集阶段，含重试，不含前期页面结构观察。','语义职能判断由助手另行核验，字段提取和匹配由核心执行。'])
    company=dict(schema_version='company-processing-1',execution_layer='browser_visible_dom',native_checkpoint=None,
                 companies=[{'company':r['company'],'status':'complete' if r['coverage_complete'] else 'blocked',
                             'scope':r['scope'],'listed':r['listed_count'],'details':r['detail_count'],'reason':r['reason']} for r in payload['reports']])
    for name,data in [('core-execution.json',audit),('company-processing.json',company)]:
        (Path(output)/name).write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')


def write_report(output,payload,receipt):
    esc=lambda value:html.escape(str(value))
    rows=''.join('<tr>'+''.join('<td>'+esc(x)+'</td>' for x in [r['company'],r['listed_count'],r['detail_count'],r['full_detail_count'],r['coverage_complete'],r['scope'],r['reason']])+'</tr>' for r in payload['reports'])
    jobs=''.join('<tr>'+''.join('<td>'+esc(x)+'</td>' for x in [r['job']['company'],r['job']['title'],r['job'].get('location_raw'),r['status'],'；'.join(k+': '+v['reason'] for k,v in r['dimensions'].items() if v['state']!='PASS')])+'<td><a href="'+esc(r['job']['source_url'])+'">官网详情</a></td></tr>' for r in payload['results'])
    page='''<!doctype html><meta charset="utf-8"><title>JobFinder 浏览器自动采集验收</title><style>body{font:16px system-ui;max-width:1400px;margin:35px auto;color:#173047;padding:0 24px}table{border-collapse:collapse;width:100%;font-size:14px}td,th{border:1px solid #cbd5e1;padding:10px;text-align:left;vertical-align:top}th{background:#e8f0f8}a{color:#125da8}h1,h2{color:#173047}</style><h1>四家企业 · 浏览器自动采集</h1>'''
    page+='<p>条件：广州 · 财务 BP · 本科及以上 · 社会招聘。时间：'+esc(payload['generated_at'])+'</p>'
    page+='<p>自动采集脚本动作 '+str(len(receipt['events']))+'；可见 DOM 快照 '+str(len(receipt['snapshots']))+'（含重试，不含前期页面结构观察）；Python HTTP 请求 0。完整详情表示职责和要求已读，薪资/经验等未披露项仍留空；不等于岗位符合或仍开放。</p>'
    page+='<p>匹配 '+str(payload['counts']['MATCH'])+'；待确认 '+str(payload['counts']['REVIEW'])+'；排除 '+str(payload['counts']['EXCLUDED'])+'。<a href="岗位汇总.xlsx">九列表格</a> · <a href="results.xlsx">工程核查表</a> · <a href="browser-run.json">浏览器证据与动作</a></p>'
    page+='<table><tr><th>企业</th><th>列表数</th><th>详情数</th><th>完整详情</th><th>范围完成</th><th>覆盖范围</th><th>问题</th></tr>'+rows+'</table><h2>详情与匹配结论</h2><table><tr><th>企业</th><th>岗位</th><th>地点</th><th>判定</th><th>原因</th><th>原文</th></tr>'+jobs+'</table>'
    page+='<h2>完整原文摘录</h2>'
    for job in payload['jobs']:
        page+='<details><summary>'+esc(job['company']+' · '+job['title']+' · '+str(job.get('location_raw') or '地点未披露'))+'</summary>'
        page+='<p>学历：'+esc(job.get('education_raw') or '未披露')+'；经验：'+esc(job.get('experience_raw') or '未披露')+'；薪资：'+esc(job.get('salary_raw') or '未披露')+'</p>'
        if job.get('role_review'):
            page+='<p>职能核验：'+esc(job['role_review']['decision']+' · '+job['role_review']['rationale'])+'</p>'
        page+='<h3>职责</h3><p style="white-space:pre-wrap">'+esc(job['description'])+'</p><h3>要求</h3><p style="white-space:pre-wrap">'+esc(job.get('requirements') or '未读取到')+'</p></details>'
    if receipt.get('previous_attempts'):
        page+='<h2>本轮重试记录</h2><p>前次快照及被替代详情保存在原始回执中，不计入最终岗位数。</p>'
        page+=''.join('<p>'+esc(a['company'])+'：'+esc(a.get('reason') or '；'.join(a.get('problems',[])) or a.get('status'))+'</p>' for a in receipt['previous_attempts'])
    (output/'report.html').write_text(page,encoding='utf-8')
