"""Typed page outcomes, conservative legacy migration and bounded retry rules."""
from datetime import datetime, timedelta, timezone
from jobfinder.models import now
import re
from urllib.parse import urlsplit


class PageIssue(ValueError):
    def __init__(self, message, kind='system_error', *, evidence=None):
        super().__init__(message)
        self.kind = kind
        self.evidence = evidence or {}


def gate_kind(text,url=''):
    if re.search(r'请完成.{0,10}验证|请拖动.{0,15}滑块|安全验证|验证您是人类|verify you are human|captcha challenge',text,re.I):
        return 'captcha'
    if re.search(r'(?:访问|操作|请求)(?:过于|太|过)?频繁|too many requests|rate limit exceeded',text,re.I):
        return 'rate_limit'
    if re.search(r'登录(?:后|即可|以)查看(?:完整|全部)|请先登录(?:后|以)?(?:查看|继续)|登录查看完整',text):
        return 'login_required'
    if re.search(r'/(?:login|passport|signin)(?:/|$)',urlsplit(url).path,re.I):
        return 'login_required'
    host=(urlsplit(url).hostname or '').lower()
    if any(host==prefix+domain for domain in ('zhipin.com','zhaopin.com','liepin.com','51job.com') for prefix in ('passport.','login.')):
        return 'login_required'
    return None


def issue_kind(exc):
    kind=getattr(exc,'kind',None)
    if kind and kind!='system_error':
        return kind
    text=str(exc)
    # Legacy records do not prove a login or CAPTCHA. Recheck dynamic pages,
    # but do not relabel ambiguous errors as confirmed human assistance.
    for needle,code in [('不允许自动读取','policy_denied'),('访问规则无法确认','policy_unknown'),
                        ('附件格式','unsupported_attachment'),('长度上限','model_truncated'),
                        ('JSON','model_format'),('域名连接失败','network_transient'),
                        ('超时或证书','network_unknown'),('浏览器发生跳转','redirect_unobserved'),
                        ('动态加载','dynamic_page'),('登录、验证或加载','dynamic_page')]:
        if needle in text:return code
    return kind or 'system_error'


HUMAN={'captcha','login_required'}
BLOCKED={'policy_denied','policy_unknown','access_denied','tls_error','unsafe_redirect','not_found'}
RECHECK={'dynamic_page','redirect_unobserved','network_transient','network_unknown','model_truncated'}


def classify_issue(exc):
    kind=issue_kind(exc)
    return {'kind':kind,'message':str(exc)[:400],
            'action':'human' if kind in HUMAN else 'skip' if kind in BLOCKED else 'system',
            'evidence':getattr(exc,'evidence',{}),'at':now()}


def terminal_status(issue):
    return 'needs_help' if issue['action']=='human' else 'blocked' if issue['action']=='skip' else 'system_error'


def ready(candidate):
    return candidate.get('retry_after','') <= now()


def retry_time():
    return (datetime.now(timezone.utc)+timedelta(seconds=3)).astimezone().replace(microsecond=0).isoformat()


def migrate_task(task):
    """One-time, no requests, no usage edits, no removal of old evidence/jobs."""
    if task.get('recovery_version')==2:return
    from .discovery import qualify_candidate
    for c in task['candidates']:
        if c['status']=='needs_help':
            issue=classify_issue(ValueError(c.get('error','未知页面异常')))
            c.setdefault('issue_history',[]).append(issue)
            c.update(issue=issue,status=terminal_status(issue),read_attempts=max(1,c.get('read_attempts',0)))
            if issue['kind'] in RECHECK:
                c['status']='pending_extract' if c.get('page_snapshot') else 'retry_pending'
                c['priority_recovery']=True
        if c['status']=='pending':
            qualify_candidate(task,c)
    task['recovery_version']=2
    task['events'].append({'at':now(),'kind':'info','message':'已升级异常分类与候选筛选；原岗位、失败记录、原文及累计预算全部保留'})
