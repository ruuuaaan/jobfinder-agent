"""Employer leads from original notices. Website candidates require separate verification."""
import hashlib
import json
import re
from urllib.parse import urlsplit
from .parsers import document, clean, links, structured_nodes
from .resources import name_key
from .network import FetchError

COMPANY = r'[\u4e00-\u9fffA-Za-z0-9（）()·\-]{2,65}?(?:股份有限公司|有限责任公司|有限公司)'
LABELS = ('公司名称', '单位名称', '招聘单位', '用人单位', '企业名称')


def visible_text(data):
    doc = document(data)
    return '\n'.join(t.strip() for t in doc.xpath('//text()[not(ancestor::script) and not(ancestor::style)]') if t.strip())


def safe_link(url):
    try:
        p = urlsplit(url)
        return p.scheme in ('http', 'https') and bool(p.hostname) and not (p.username or p.password) and p.port in (None, 80, 443)
    except ValueError:
        return False


def extract_employers(data, source_url, retrieved_at, snapshot=None):
    """Only employer labels, recruiting headings/table columns, or JobPosting organizations."""
    doc, text, found = document(data), visible_text(data), {}

    def add(value, quote, method):
        value = clean(value or '') or ''
        if re.fullmatch(COMPANY, value) and name_key(value) not in found:
            found[name_key(value)] = dict(legal_name=value, company_name=value, source_url=source_url,
                acquired_at=retrieved_at, version_date='unknown', list_date=None,
                discovery_route='announcement_reverse', employer_quote=quote[:600],
                extraction_method=method, official_url=None, career_url=None,
                content_hash=hashlib.sha256(data).hexdigest(), snapshot=snapshot)

    for script in doc.xpath('//script[@type="application/ld+json"]'):
        try:
            for node in structured_nodes(json.loads(script.text or '{}')):
                org = node.get('hiringOrganization', {})
                if isinstance(org, dict):
                    add(org.get('name'), json.dumps(org, ensure_ascii=False), 'jobposting_employer')
        except (ValueError, TypeError):
            continue
    for label in LABELS:
        for match in re.finditer(re.escape(label) + r'\s*[：:]?\s*(' + COMPANY + ')', text):
            add(match[1], match[0], 'labelled_employer')
    for node in doc.xpath('//h1|//h2|//h3|//title'):
        heading = clean(node.text_content()) or ''
        match = re.search(r'(?:^|[\s：:、])(' + COMPANY + r')(?=\s|招聘|20\d{2}|$)', heading)
        if match and ('招聘' in heading or '招聘' in text):
            add(match[1], heading, 'recruiting_heading')
    for table in doc.xpath('//table'):
        employer_index = None
        industry_index = None
        for row in table.xpath('.//tr'):
            cells = [clean(c.text_content()) or '' for c in row.xpath('./td|./th')]
            for i, cell in enumerate(cells):
                if cell.rstrip('：: ') in ('单位行业','所属行业','行业类别','行业'):
                    industry_index = i
                if cell.rstrip('：: ') in LABELS:
                    employer_index = i
                    if i+1 < len(cells):
                        add(cells[i+1], ' | '.join(cells), 'employer_table')
            if employer_index is not None and employer_index < len(cells):
                add(cells[employer_index], ' | '.join(cells), 'employer_table')
                record = found.get(name_key(cells[employer_index]))
                if record is not None and industry_index is not None and industry_index < len(cells):
                    record['industry'] = cells[industry_index] or None
                    record['industry_quote'] = ' | '.join(cells)[:600]
    website_links = []
    for a in links(data, source_url):
        if re.search(r'公司官网|企业官网|公司网站|公司网址|官方网站|官网', a['title'] or '') and safe_link(a['url']):
            if urlsplit(a['url']).hostname != urlsplit(source_url).hostname:
                website_links.append(a['url'])
    for match in re.finditer(r'(?:公司官网|企业官网|公司网站|公司网址|官方网站)\s*[：:]?\s*((?:https?://|www\.)[^\s<>"，。；]+)', text):
        target = match[1] if match[1].startswith(('http://', 'https://')) else 'http://' + match[1]
        if safe_link(target):
            website_links.append(target)
    website_links = list(dict.fromkeys(website_links))
    result = list(found.values())
    if len(result) == 1 and len(website_links) == 1:
        result[0]['official_url'] = website_links[0]
    for company in result:
        company['website_candidates'] = website_links if len(result) == 1 else []
        company['official_status'] = 'candidate' if company['official_url'] else 'unknown'
    return result


def read_announcement(fetcher, url, store=None, selected_names=None):
    policy = fetcher.policies.get(urlsplit(url).hostname, {})
    if not policy.get('publisher_verified') or not policy.get('publisher_evidence_url'):
        raise FetchError('policy_pending', '公告发布方尚未核验')
    page = fetcher.get(url)
    companies = extract_employers(page['data'], url, page['retrieved_at'], page['snapshot'])
    page['employers_found'] = len(companies)
    if selected_names:
        selected = {name_key(n) for n in selected_names}
        companies = [c for c in companies if name_key(c['legal_name']) in selected]
    if store:
        for company in companies:
            store.add(company['legal_name'], url, page['retrieved_at'], 'announcement_original',
                      values={'official_url': company['official_url'], 'industry': company.get('industry')},
                      supported=('industry',) if company.get('industry') else (),
                      evidence={'quote': company.get('industry_quote') or company['employer_quote'], 'content_hash': page['hash'], 'snapshot': page['snapshot']},
                      context={'extraction_method': company['extraction_method']})
    return companies, page


def verify_announcement_employer(fetcher, candidate, store=None):
    companies, page = read_announcement(fetcher, candidate.announcement_url, store, [candidate.company] if candidate.company else None)
    matched = next((c for c in companies if name_key(c['legal_name']) == name_key(candidate.company or '')), None)
    if not matched:
        raise FetchError('employer_unverified', '公告原文未能确认指定招聘企业，不能用搜索摘要代替')
    domains = {urlsplit(url).hostname.lower().removeprefix('www.') for url in matched['website_candidates']}
    if domains and (urlsplit(candidate.official_url or '').hostname or '').lower().removeprefix('www.') not in domains:
        raise FetchError('official_unverified', '候选官网与公告明确给出的官网域名不一致，须核实主体关系')
    return {'source_url': candidate.announcement_url, 'company': matched['legal_name'],
            'quote': matched['employer_quote'], 'content_hash': page['hash'], 'snapshot': page['snapshot'],
            'retrieved_at': page['retrieved_at'], 'website_candidates': matched['website_candidates']}


def company_profile(data, company_name):
    text = visible_text(data)
    if not name_key(company_name or '') or name_key(company_name) not in name_key(text):
        return None
    paragraphs = [' '.join(t.split()) for t in re.split(r'[\n。]', text) if t.strip()]
    other_names = [n for n in re.findall(COMPANY, text) if name_key(n) != name_key(company_name)]
    def belongs(t):
        if t.startswith(company_name):
            remainder = t[len(company_name):]
            return not re.search(COMPANY, remainder) and not re.search(r'子公司|母公司|旗下|客户|合作伙伴', remainder)
        return not other_names and t.startswith(('公司主营', '公司主要业务', '公司是一家', '公司专注', '公司致力', '公司在'))
    own = [t for t in paragraphs if belongs(t)]
    intro = next((t[:1200] for t in own if len(t) >= 20 and
                  re.search(r'主营|主要业务|是一家|专注于|致力于|业务涵盖|成立于', t)), None)
    business_quote = next((t[:1200] for t in own if re.search(r'主营|主要业务|专注于|致力于|业务涵盖|从事', t)), None)
    business_match = re.search(r'(?:主营(?:业务)?|主要业务(?:为|是)?|专注于|致力于|业务涵盖|从事)\s*[：:]?\s*(.+)', business_quote or '')
    business = business_match[1][:600] if business_match else None
    industry_text = '\n'.join(own) if other_names else text
    industry = re.search(r'(?:所属行业|行业类别|所属领域)\s*[：:]\s*([^\n。；]{2,50})', industry_text)
    locations, location_quotes = [], []
    from .geography import normalize_city
    kinds = {'办公室':'office', '分公司':'branch', '工厂':'factory', '门店':'store', '研发中心':'research_center', '运营中心':'operating_site'}
    for t in own:
        if re.search(r'计划|拟|将|筹建|撤销|关闭|曾经|不再|未设', t):
            continue
        match = re.search(r'在([\u4e00-\u9fff]{2,8}?)\s*(?:设有|运营|开设了|设立了)(?:一家|一处|一个)?(办公室|分公司|工厂|门店|研发中心|运营中心)', t)
        if match:
            try:
                city = normalize_city(match[1])
            except ValueError:
                continue  # Enrichment must not break job reading for an ambiguous place.
            if not city:
                continue
            locations.append({'city':city, 'unit_name':company_name + city + match[2], 'kind':kinds[match[2]]})
            location_quotes.append(t)
    return {'company_intro':intro, 'main_business':business, 'industry':industry[1].strip() if industry else None,
            'operating_locations':locations,
            'quotes':{'company_intro':intro, 'main_business':business_quote,
                      'industry':industry[0] if industry else None, 'operating_locations':'；'.join(location_quotes)},
            'identity_quote': next((t[:600] for t in paragraphs if name_key(company_name) in name_key(t)), company_name)}


def record_profile(store, company_name, source_url, page, official_url=None):
    profile = company_profile(page['data'], company_name)
    if not profile:
        return False
    if store:
        for field in ('industry', 'main_business', 'company_intro', 'operating_locations'):
            if profile[field]:
                store.add(company_name, source_url, page['retrieved_at'], 'official_profile',
                          values={field: profile[field]}, supported=(field,),
                          evidence={'quote': profile['quotes'][field], 'content_hash': page['hash'], 'snapshot': page['snapshot'],
                                    'entity_name':company_name, 'subject_relation':'self',
                                    'kind':'operating_unit' if field=='operating_locations' else 'profile'})
        if official_url:
            store.add(company_name, source_url, page['retrieved_at'], 'official_identity',
                      values={'official_url': official_url}, supported=('official_url',),
                      evidence={'quote': profile['identity_quote'], 'content_hash': page['hash'], 'snapshot': page['snapshot']})
    return True
