"""Small, sample-verified adapters; no JavaScript execution and no inferred job fields."""
import re
from urllib.parse import urlsplit
from .models import Job


def parse_sections(data, candidate, snapshot=None, retrieved_at=None):
    from .parsers import document, clean, enrich
    doc=document(data)
    host=urlsplit(candidate.url).hostname
    full='\n'.join(t.strip() for t in doc.xpath('//text()[not(ancestor::script) and not(ancestor::style)]') if t.strip())
    output=[]
    if host=='www.bohui.com.cn':
        company='北京市博汇科技股份有限公司'
        if company not in full:
            return []
        for block in doc.xpath('//div[@class="job-item"]'):
            titles=block.xpath('./div[@class="link"]/h2[@class="tit"]')
            place=block.xpath('./div[@class="link"]/span')
            duties=block.xpath('.//div[@class="ms"]//div[@class="info-wrap"]')
            req=block.xpath('.//div[@class="need"]//div[@class="info-wrap"]')
            if not titles or not duties:
                continue
            title=clean(titles[0].text_content())
            location=re.sub(r'^工作地点\s*[：:]\s*','',clean(place[0].text_content())) if place else None
            requirements=clean(req[0].text_content()) if req else None
            emails=re.findall(r'[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}',requirements or '')
            output.append(Job(company=company,title=title,description=clean(duties[0].text_content()),requirements=requirements,
                location_raw=location,source_url=candidate.url,source_channel=candidate.source_channel,
                source_id=title,application_method='邮箱：'+emails[0] if emails else None,
                location_mode='unknown' if location and re.search(r'[、/]',location) else 'single',
                notes=['公告未明确发布日期、截止日期及当前开放状态；多地未明示独立选择时保留UNKNOWN'],synthetic=candidate.synthetic))
    elif host=='www.sungoin.com':
        company='商客通尚景科技（上海）股份有限公司'
        if company not in full:
            return []
        places={}
        for a in doc.xpath('//div[contains(@class,"num_con_tit")]/a'):
            spans=a.xpath('./span')
            if len(spans)==2:
                places.setdefault(clean(spans[0].text_content()),set()).add(clean(spans[1].text_content()))
        for block in doc.xpath('//div[@class="tab-con"]'):
            headings=block.xpath('./h1')
            duties=block.xpath('./h2[contains(.,"岗位职责")]/following-sibling::ul[1]')
            req=block.xpath('./h2[contains(.,"岗位要求")]/following-sibling::ul[1]')
            if not headings or not duties:
                continue
            title=clean(headings[0].text_content())
            location=next(iter(places[title])) if title in places and len(places[title])==1 else None
            output.append(Job(company=company,title=title,description=clean(duties[0].text_content()),requirements=clean(req[0].text_content()) if req else None,
                location_raw=location,source_url=candidate.url,source_channel=candidate.source_channel,source_id=title,
                notes=['地点通过同页岗位名称唯一对应表关联，不使用公司地址','原文邮箱仅为咨询用途，不冒充投递入口','未披露薪资和明确开放时限'],synthetic=candidate.synthetic))
    return [enrich(j,data,'html_sections_v1',snapshot,retrieved_at) for j in output]
