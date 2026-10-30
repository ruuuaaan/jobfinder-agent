"""Conservative employer category evidence from original company introduction text."""
import re
from .models import Evidence
from .parsers import document


def scope_evidence(page, source_url, company, keywords):
    doc = document(page['data'])
    blocks = [re.sub(r'\s+', ' ', x.text_content()).strip() for x in doc.xpath('//p|//li|//h1|//h2|//h3|//div[not(div)]') if not x.xpath('ancestor::script|ancestor::style')]
    quotes = {}
    for keyword in keywords:
        for text in blocks:
            # Bind category to the named employer, not to required experience/clients.
            if company not in text or len(text) > 1800 or not re.search(re.escape(keyword), text, re.I):
                continue
            if re.search(r'非MCN|不是MCN|不属于MCN|MCN.{0,8}经验|MCN.{0,8}优先|服务MCN|MCN.{0,8}客户', text, re.I):
                continue
            if not re.search(r'(?:是一家|作为|专注|旗下|业务|机构|公司简介|关于我们)', text):
                continue
            quotes['company_scope:'+keyword.casefold()] = text
            break
    if not quotes:
        return None
    return Evidence(source_url=source_url,retrieved_at=page['retrieved_at'],content_hash=page['hash'],snapshot=page['snapshot'],method='company_profile_original',field_quotes={'company':company,**quotes})
