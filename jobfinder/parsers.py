"""Only original page content is parsed; candidate titles never become job fields."""
from __future__ import annotations
import hashlib
import json
import re
from urllib.parse import urljoin, urlsplit
from lxml import html
from .models import Candidate, Evidence, Job


class ParseError(ValueError):
    pass


def document(data: bytes):
    # libxml honors the original HTML charset declaration (including GB2312).
    if not data.strip():
        raise ParseError("空响应")
    if not re.search(rb'charset\s*=',data[:5000],re.I):
        try:
            return html.fromstring(data.decode('utf-8-sig'))
        except UnicodeDecodeError:
            pass
    return html.fromstring(data)


def clean(value):
    if value is None:
        return None
    if "<" in str(value):
        try:
            value = html.fromstring(str(value)).text_content()
        except Exception:
            pass
    return re.sub(r"[ \t\r\f\v]+", " ", str(value)).strip()


def links(data: bytes, base: str):
    doc = document(data)
    return [{"title": clean(a.text_content()), "url": urljoin(base, a.get("href"))}
            for a in doc.xpath("//a[@href]") if not a.get("href").startswith(("javascript:", "#"))]


def structured_nodes(obj):
    if isinstance(obj, list):
        for node in obj:
            yield from structured_nodes(node)
    elif isinstance(obj, dict):
        types = obj.get("@type", [])
        if types == "JobPosting" or isinstance(types, list) and "JobPosting" in types:
            yield obj
        if "@graph" in obj:
            yield from structured_nodes(obj["@graph"])


def enrich(job, data, method, snapshot=None, retrieved_at=None):
    req = job.requirements or ""
    if not job.education_raw:
        m = re.search(r"[^。；\n]{0,30}(?:学历不限|不限学历|本科|大专|专科|硕士|博士|高中|中专|初中|小学|中技|职高)[^。；\n]{0,50}", req)
        job.education_raw = m[0] if m else None
    if not job.experience_raw:
        m = re.search(r"[^。；\n]{0,12}(?:\d+\s*年[^。；\n]{0,20}经验|经验不限)[^。；\n]{0,15}", req)
        job.experience_raw = m[0] if m else None
    if not job.level:
        m = re.search(r"高级|资深|初级|中级|总监|senior|junior|lead", job.title, re.I)
        job.level = m[0] if m else None
    fields = {key: str(getattr(job, key)) for key in ["company", "title", "description", "requirements", "location_raw", "education_raw", "experience_raw", "salary_raw", "posted_at", "deadline", "application_url", "application_method"] if getattr(job, key) is not None}
    kwargs = {"retrieved_at": retrieved_at} if retrieved_at else {}
    job.evidence.append(Evidence(source_url=job.source_url, content_hash=hashlib.sha256(data).hexdigest(), method=method, snapshot=snapshot, field_quotes=fields, **kwargs))
    return job


def parse_html(data, candidate: Candidate, snapshot=None, retrieved_at=None):
    doc = document(data)
    jobs = []
    for script in doc.xpath('//script[@type="application/ld+json"]'):
        try:
            objects = list(structured_nodes(json.loads(script.text or "{}")))
        except (ValueError, TypeError):
            continue
        for node in objects:
            org = node.get("hiringOrganization", {})
            company = org.get("name") if isinstance(org, dict) else None
            if not company or not node.get("title") or not node.get("description"):
                continue
            raw_locations = node.get("jobLocation", [])
            raw_locations = raw_locations if isinstance(raw_locations, list) else [raw_locations]
            places = []
            for loc in raw_locations:
                address = loc.get("address", {}) if isinstance(loc, dict) else {}
                if isinstance(address, str):
                    places.append(address)
                elif isinstance(address, dict):
                    places.append(" ".join(str(address.get(k, "")) for k in ["addressCountry", "addressRegion", "addressLocality", "streetAddress"]).strip())
            sal = node.get("baseSalary", {})
            sal = sal if isinstance(sal, dict) else {}
            value = sal.get("value", {})
            value = value if isinstance(value, dict) else {}
            salary_raw = json.dumps(sal, ensure_ascii=False) if sal else None
            if value.get("minValue") is not None and value.get("maxValue") is not None:
                salary_raw = f"{value['minValue']}-{value['maxValue']} {sal.get('currency', '')} {value.get('unitText', '')}"
            remote = node.get("jobLocationType") == "TELECOMMUTE"
            job = Job(company=company, title=node["title"], description=clean(node["description"]), requirements=clean(node.get("qualifications") or node.get("educationRequirements")),
                      location_raw="remote " + ";".join(places) if remote else ";".join(places) or None,
                      locations=places, location_mode="alternatives" if len(places)>1 else "single", work_mode="remote" if remote else "unknown",
                      salary_raw=salary_raw, salary_component="fixed" if sal else "unknown", salary_currency=sal.get("currency"), salary_period={"MONTH":"month", "YEAR":"year", "HOUR":"hour", "DAY":"day"}.get(value.get("unitText")),
                      source_url=candidate.url, source_channel=candidate.source_channel,
                      source_id=str(node.get("identifier", {}).get("value")) if isinstance(node.get("identifier"), dict) and node["identifier"].get("value") is not None else None,
                      posted_at=node.get("datePosted"), deadline=node.get("validThrough"), recruitment_type=node.get("employmentType") if isinstance(node.get("employmentType"), str) else None,
                      synthetic=candidate.synthetic)
            # url/directApply do NOT guarantee a distinct application endpoint.
            if isinstance(node.get('url'),str):
                job.detail_url=urljoin(candidate.url,node['url'])
            jobs.append(enrich(job, data, "json_ld", snapshot, retrieved_at))
    if jobs:
        return jobs
    from .moka import parse_bootstrap
    embedded=parse_bootstrap(doc,data,candidate,snapshot,retrieved_at)
    if embedded:
        return embedded
    host = urlsplit(candidate.url).hostname
    if host == "corp.160.com" and re.fullmatch(r"/hr/detail-\d+-\d+\.html", urlsplit(candidate.url).path):
        headers = doc.xpath("//h3")
        title = next((clean(h.text_content()) for h in headers if clean(h.text_content())), None)
        text = "\n".join(t.strip() for t in doc.xpath("//text()[not(ancestor::script) and not(ancestor::style)]") if t.strip())
        company = re.search(r"深圳市驱动人生科技股份有限公司", text)
        place = re.search(r"工作地点[：:]\s*([^\s\n]+)", text)
        if not title or not company or "岗位职责" not in text or "任职要求" not in text:
            raise ParseError("驱动人生具体岗位结构不完整")
        duties = re.split(r"岗位职责[：:]?", text, maxsplit=1)[1].split("任职要求")[0].strip()
        requirements = re.split(r"任职要求[：:]?", text, maxsplit=1)[1].split("简历超过")[0].strip()
        emails = re.findall(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}", text)
        method = "邮箱：" + emails[0] if emails else None
        job = Job(company=company[0], title=title, description=duties, requirements=requirements,
                  location_raw=place[1] if place else None, source_url=candidate.url,
                  source_channel=candidate.source_channel, source_id=urlsplit(candidate.url).path,
                  application_method=method, notes=["网页有简历表单但未验证提交能力；仅记录邮箱方式，未发送", "页面未给出发布日期/截止日期，开放状态未知"], synthetic=candidate.synthetic)
        return [enrich(job, data, "html_corp160_v1", snapshot, retrieved_at)]
    # General explicit key-value job table, e.g. employer notices and Nubia.
    fields = {}
    for row in doc.xpath("//tr"):
        cells = [clean(x.text_content()) for x in row.xpath("./th|./td")]
        for i, cell in enumerate(cells[:-1]):
            key = cell.rstrip("：: ")
            if key in ["职位名称", "工作地点", "工作职责", "工作要求", "招聘渠道", "薪资", "学历要求"]:
                fields[key] = cells[i+1]
    if fields.get("职位名称") and fields.get("工作职责"):
        text = clean(doc.text_content())
        company = re.search(r"[\u4e00-\u9fff（）()]{2,40}?(?:股份有限公司|有限责任公司|有限公司)", text)
        if not company:
            raise ParseError("岗位表存在，但招聘主体在原文无法确认")
        apply_links = [a for a in links(data, candidate.url) if a["title"] in ["投递简历", "立即申请", "我要应聘"] and a["url"].startswith("https://")]
        job = Job(company=company[0], title=fields["职位名称"], description=fields["工作职责"], requirements=fields.get("工作要求"), location_raw=fields.get("工作地点"), education_raw=fields.get("学历要求"), salary_raw=fields.get("薪资"), recruitment_type=fields.get("招聘渠道"), source_url=candidate.url, source_channel=candidate.source_channel, application_url=apply_links[0]["url"] if apply_links else None, application_method="网页链接（未提交）" if apply_links else None, synthetic=candidate.synthetic)
        return [enrich(job, data, "html_key_value_table", snapshot, retrieved_at)]
    from .site_parsers import parse_sections
    jobs=parse_sections(data,candidate,snapshot,retrieved_at)
    if jobs:
        return jobs
    raise ParseError("未找到支持的具体岗位结构（不把首页、列表或搜索摘要作为岗位）；可能需附件/站点专用适配器")
