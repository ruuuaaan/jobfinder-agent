"""Observe native filters and pagination. Never construct website query parameters."""
import re
from urllib.parse import urljoin, urlsplit
from .parsers import document, clean
from .geography import normalize_city
from .input_rules import recruitment_terms
from .network import request_url


def controls(page, base):
    doc = document(page['data'])
    next_links, pagination, terminal = [], set(), []
    # Filter options are navigation controls, never individual vacancy details.
    for box in doc.xpath('//*[@data-filter or @aria-label]'):
        if (box.get('data-filter') or box.get('aria-label')) not in ('city','城市','工作地点','location','recruitment_type','招聘类型','招聘类别','education','学历'):
            continue
        for anchor in box.xpath('.//a[@href]'):
            raw=anchor.get('href')
            if raw and not raw.startswith(('#','javascript:')):
                pagination.add(request_url(urljoin(base,raw)))
    for element in doc.xpath('//a|//button|//span'):
        label = clean(element.text_content()) or ''
        rel = element.get('rel','').lower().split()
        is_next = 'next' in rel or bool(re.fullmatch(r'(?:下一页|下页|Next(?:\s+page)?|加载更多|Load\s+more)[\s>»›]*',label,re.I))
        parent_pager = any(re.search(r'pagination|pager',p.get('class',''),re.I) for p in element.iterancestors())
        is_previous = 'prev' in rel or bool(re.fullmatch(r'(?:上一页|上页|首页|末页|Previous(?:\s+page)?|Prev|First|Last)[\s<«‹>»›]*',label,re.I))
        if not is_next and not is_previous and not parent_pager:
            continue
        disabled = element.get('disabled') is not None or element.get('aria-disabled')=='true' or any('disabled' in p.get('class','').split() for p in [element,element.getparent()] if p is not None)
        raw = element.get('href')
        if raw and not raw.startswith(('#','javascript:')):
            url=urljoin(base,raw);pagination.add(request_url(url))
            if not disabled and is_next:
                next_links.append({'url':url,'title':label,'kind':'list'})
        elif is_next and not disabled:
            next_links.append({'url':None,'title':label,'kind':'browser'})
        if is_next and disabled:
            terminal.append({'kind':'disabled_next','quote':label,'source_url':base,'content_hash':page['hash']})
    text=clean(doc.text_content())
    for current,total in re.findall(r'第\s*(\d+)\s*页\s*[/，,]?\s*共\s*(\d+)\s*页',text):
        if int(current)==int(total) and int(total)>0:
            terminal.append({'kind':'last_page_number','quote':f'第{current}页 共{total}页','source_url':base,'content_hash':page['hash']})
    # Detail pages never use their unrelated page controls as recruitment pagination.
    return next_links,pagination,terminal


def native_filters(page, base, config, applied=()):
    if config is None:
        return [],[]
    doc=document(page['data']); audits=[]
    groups=[('city',config.target_cities,('城市','工作地点','city','location')),
            ('recruitment_type',recruitment_terms(config.recruitment_type),('招聘类型','recruitment_type','招聘类别'))]
    for dimension,wanted,labels in groups:
        if not wanted or dimension in applied:
            continue
        options={}
        containers=doc.xpath('//*[@data-filter or @aria-label]')
        for box in containers:
            if (box.get('data-filter') or box.get('aria-label')) not in labels:
                continue
            for anchor in box.xpath('.//a[@href]'):
                label=clean(anchor.text_content())
                terms=recruitment_terms(label) if dimension=='recruitment_type' else []
                key=normalize_city(label) if dimension=='city' else terms[0] if len(terms)==1 else None
                raw=anchor.get('href')
                if key and raw and not raw.startswith(('#','javascript:')):
                    options[key]={'url':urljoin(base,raw),'title':label,'dimension':dimension}
        normalized=[normalize_city(x) if dimension=='city' else x for x in wanted]
        if options and all(x in options for x in normalized):
            return [options[x] for x in dict.fromkeys(normalized)],audits
        audits.append({'dimension':dimension,'action':'local_filter','reason':'no_observed_equivalent_link_options'})
    if config.education.enabled:
        audits.append({'dimension':'education','action':'local_filter','reason':'single_degree_option_not_equivalent_to_threshold_and_unknowns'})
    return [],audits
