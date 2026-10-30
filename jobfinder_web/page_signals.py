"""Observable career-page signals, shared by HTTP and visible-browser readers.

These signals select how to read a page. They never establish job matching,
employer identity, or whether recruitment is still open.
"""
import re
from urllib.parse import unquote, urlsplit


def is_job_detail_url(url):
    parts = urlsplit(url)
    path = unquote(parts.path)
    if re.search(r'/(?:jobs?|positions?)/(?:search|list|social|campus)/?$', path, re.I):
        return False
    return bool(re.search(
        r'/job_detail/|/jobs?/[^/]+|/detail/job|/jobdetail(?:/|\.)|/posDetail(?:/|\.)|'
        r'/positions?/detail|/CC\d|webPosition[^/]*[!]getOnePosition', path, re.I)
        or re.search(r'(?:^|&)(?:jobId|positionId|postId|postIdEnc)=[^&]+', parts.query, re.I))


def is_career_url(url):
    parts = urlsplit(url)
    host = parts.hostname or ''
    path = unquote(parts.path)
    career_host = bool(re.search(r'(?:^|\.)(?:careers?|recruit(?:ment)?|zhaopin|jobs)\.', host, re.I))
    if re.search(r'/(?:about|privacy|login|register|signin|account|favorite)(?:[/_.-]|$)', path, re.I):
        return False
    return bool((career_host and (path in ('', '/') or re.search(r'/(?:social|campus|intern|search|index|apply)(?:[/_.-]|$)', path, re.I)))
        or re.search(r'/(?:careers?|recruit(?:ment)?|zhaopin|shehuizhaopin|rencaizhaopin|joinus|jobs|positions)(?:[/_.-]|$)|'
                     r'/pc_recruit\.|/wt/[^/]+/(?:mob)?web/', path, re.I))


def page_readiness(page):
    """Do not mistake a long menu/filter table for loaded vacancy content."""
    text = re.sub(r'\s+', ' ', page.get('text', '')).strip()
    # A primary expired-vacancy notice takes precedence over footer suggestions.
    # Restrict to the leading page content; a closed recommendation is not proof
    # that the current vacancy is closed.
    notice = re.search(r'(?:抱歉[，,]?\s*)?(?:(?:该|此|本|您访问的|你访问的|您查找的)\s*)?'
                       r'(?:职位|岗位|工作机会)(?:已经|已|现已)?\s*(?:过期(?:或者不存在)?|不存在|已结束|已关闭|已下线|停止招聘)|'
                       r'(?:this (?:job|position|vacancy) (?:has expired|is no longer available|is closed))', text[:800], re.I)
    # Without a reference to this vacancy, require an unpopulated page. This
    # avoids treating a generic list containing closed cards as a closed job.
    if notice and not re.search(r'岗位职责|任职要求|相关推荐|相似职位|推荐职位',text[:notice.start()]) and (is_job_detail_url(page.get('url','')) or re.search(r'抱歉|该|此|你访问|您访问|this ',notice[0],re.I)):
        return {'state':'vacancy_closed','reason':'原页明确显示岗位已过期、关闭或不存在','quote':notice[0]}
    path = unquote(urlsplit(page.get('url', '')).path)
    listing = bool(re.search(r'职位列表|岗位列表|职位搜索|搜索职位|岗位搜索|搜索岗位|job search|job listings', text, re.I)
        or re.search(r'/(?:positions?/list|social/jobs|jobs|careers/search)(?:[/.]|$)', path, re.I)
        or (re.search(r'职位名称|岗位名称|job title', text, re.I)
            and re.search(r'工作地点|职位类别|所属机构|招聘人数|location', text, re.I)))
    if re.search(r'enable javascript|请开启\s*javascript|请启用\s*javascript', text, re.I):
        return {'state': 'loading_shell', 'reason': '页面明确要求启用 JavaScript'}
    # A footer may still say Loading after genuine cards/details have appeared.
    # Actual observed detail links and substantive duties take precedence.
    details = any(is_job_detail_url(link.get('url', '')) and link.get('url')!=page.get('url') for link in page.get('links', []))
    sections = re.findall(r'(?:岗位职责|工作职责|职责描述|职位描述|职位介绍|工作描述|任职要求|职位要求)\s*[:：]?\s*(.{20,300})', text)
    duties = any(re.search(r'负责|协助|参与|制定|完成|具备|熟悉|掌握|本科|大专|硕士|博士', section) for section in sections)
    # Some sites render cards with buttons instead of anchors. A salary plus
    # an education/experience requirement is also evidence of a populated card.
    cards = bool(re.search(r'\d+(?:\.\d+)?\s*[-~～—至]\s*\d+(?:\.\d+)?\s*(?:[kKwW千万元]|万/年)', text)
        and re.search(r'本科|大专|硕士|博士|高中|学历不限|经验不限|\d+\s*年', text))
    if details or duties or cards:
        return {'state': 'job_content', 'reason': '观察到岗位详情链接、职责正文或职位卡片'}
    if listing and re.search(r'暂无(?:符合.{0,15})?(?:相关)?(?:职位|岗位|招聘信息|数据)|'
                             r'没有(?:找到)?(?:符合.{0,20})?(?:相关)?(?:职位|岗位)|'
                             r'未(?:找到|搜索到|查询到).{0,15}(?:职位|岗位)|'
                             r'共\s*0\s*(?:个|条)?\s*(?:职位|岗位)|'
                             r'no (?:matching )?(?:jobs|positions|vacancies|results)', text, re.I):
        return {'state': 'empty_results', 'reason': '页面明确显示当前条件没有职位；不代表整家企业没有岗位'}
    if listing:
        return {'state': 'loading_shell', 'reason': '只读到职位列表框架或筛选项，尚未取得职位内容或明确的无结果提示'}
    return {'state': 'content', 'reason': '普通页面内容，仍需继续核验招聘入口和岗位'}
