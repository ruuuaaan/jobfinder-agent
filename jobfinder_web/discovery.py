"""Rank observed links; retain rejected discovery records with an explicit reason."""
import re
from datetime import date
from urllib.parse import urlsplit
from .page_signals import is_job_detail_url, is_career_url

SELECTION_VERSION=5


def folded(s):
    return re.sub(r'\s+','',str(s)).casefold()


def link_decision(task, item, parent=None):
    title=str(item.get('title') or item.get('text') or '')
    url=item['url']; parts=urlsplit(url)
    terms=task['input']['terms']+task['input'].get('adjacent_terms',[])
    related=any(folded(t) in folded(title) for t in terms)
    same=not parent or parts.hostname==urlsplit(parent).hostname
    detail=is_job_detail_url(url)
    if parent and url.split('#')[0]==parent.split('#')[0]:
        return False,'当前页面自身链接，无需重复读取',99
    if re.search(r'隐私|用户协议|注册账号|下载APP|薪酬报告|求职攻略|职场资讯|收藏职位|收藏岗位|投递记录|我的简历',title,re.I):
        return False,'导航或非岗位内容',99
    if re.search(r'如何成为|系列分享|入门教程|岗位职责模板|工作总结范文|培训课程',title) and not re.search('招聘|招募|诚聘|投递',title):
        return False,'知识文章或模板，未显示招聘线索',95
    if not detail and re.search(r'白皮书|年度报告|数智化转型|转型重构|工作逻辑|税务管理组织架构|财务BP是什么|如何做好|岗位职责范文',title,re.I) and not re.search('招聘|招募|诚聘|投递',title):
        return False,'行业报告或职能知识文章，缺少实际招聘线索',95
    years=[int(y) for y in re.findall(r'(?<!\d)(20\d{2})年?',title)]
    if years and max(years)<date.today().year-1 and re.search('公告|招聘会|招聘信息|校园招聘|春风行动|就业援助|招聘专场|制造业专场',title):
        return False,'旧公告线索暂缓；保留来源，不推断具体岗位已关闭',90
    if not detail and re.search('专题专栏|政策解读|实施方案|行动方案|统计公报|行业研究报告',title) and not re.search('招聘岗位|招聘简章|人才招聘|诚聘|招募',title):
        return False,'政策、统计或行业栏目，不是本轮招聘线索',95
    if not parent:
        return True,'直接搜索或已选企业入口',0 if detail and related else 1 if related else 3
    if related:
        return True,'链接标题命中本次岗位词',0 if detail else 2
    if same and re.fullmatch(r'下一页|下页|next(?:\s*page)?|[>›»]+',title.strip(),re.I):
        return True,'同站已观察下一页',2
    if same and re.fullmatch(r'加入我们|社会招聘|校园招聘|招聘职位|招聘岗位|人才招聘|招聘|careers|jobs',title.strip(),re.I):
        # Career navigation can live below a home page (for example /talent).
        # Do not go back to every recruitment menu from individual details.
        if not is_job_detail_url(parent):
            return True,'页面已观察招聘入口',1
    if detail and (not title.strip() or re.fullmatch(r'职位详情|岗位详情|查看详情|详情|申请职位|投递简历|view (?:job|details)',title.strip(),re.I)):
        return True,'已观察详情入口，职责待核验',1
    if is_career_url(url) and not detail and not is_job_detail_url(parent) and not re.search(r'其他|全国|热门|推荐|所有行业',title):
        return True,'已观察招聘网址，企业归属与具体岗位待核验',2
    if same and parts.path.lower().endswith(('.pdf','.docx','.xlsx')) and re.search('附件|岗位|职位',title):
        return True,'原页附件待读取',3
    return False,'展开链接缺少本次岗位关联，暂缓读取',90


def extraction_decision(page):
    """Keep the snapshot and observed career links, skip clearly non-vacancy text."""
    import re
    title=page.get('title','');body=page.get('text','')
    if is_job_detail_url(page['url']):return True,''
    if re.search(r'岗位职责|任职要求|职位描述|招聘岗位|招聘职位|诚聘|招聘简章|申请职位',body):return True,''
    if re.search(r'专题专栏|政策解读|实施方案|统计公报|关于我们|公司简介|企业介绍|联系我们|网站地图',title):
        return False,'页面为政策/企业介绍/导航，未显示具体岗位正文；招聘链接已另入队'
    if page.get('readiness',{}).get('state')=='empty_results':return False,'原页明确没有当前条件的职位'
    return True,''


def qualify_candidate(task,c):
    keep,reason,priority=link_decision(task,c,c.get('parent_url'))
    c['selection']={'eligible':keep,'reason':reason,'priority':priority,'version':SELECTION_VERSION}
    if not keep:c['status']='filtered'
    return keep


def refresh_pending(task):
    for c in task['candidates']:
        if c['status']!='pending' or c.get('page_snapshot') or c.get('resource_provenance') or c.get('selection',{}).get('version',0)>=SELECTION_VERSION:continue
        c.setdefault('selection_history',[]).append(c.get('selection',{}))
        qualify_candidate(task,c)
