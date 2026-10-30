"""Local login evidence and per-task route choices. Never stores cookies or account text."""
from contextlib import closing
import hashlib
import json
import sqlite3
import time
from pathlib import Path
from jobfinder.models import now
from .site_access import channel_url, site_key

PLATFORMS = ('zhaopin', 'boss', 'liepin', '51job')
CHECK_TTL_SECONDS = 600


def channel_for(url):
    return next((c for c in PLATFORMS if site_key(channel_url(c)) == site_key(url)), None)


class LoginRegistry:
    def __init__(self, folder):
        self.path = Path(folder) / 'login-state.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS login_states(channel TEXT PRIMARY KEY, body TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS login_checks(id INTEGER PRIMARY KEY, body TEXT NOT NULL)')

    def save(self, channel, result, session_id=None):
        # Whitelist metadata: never persist DOM, username, full redirects, credentials or cookies.
        body = {'channel': channel, 'url': channel_url(channel), 'state': result['state'],
                'reason': result.get('reason', ''), 'checked_at': now(), 'checked_epoch': time.time(),
                'session_id': session_id, 'evidence': {k: bool(v) for k, v in result.get('evidence', {}).items()
                    if k in ('logout_control', 'account_identity', 'private_navigation', 'login_control', 'challenge')}}
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('INSERT OR REPLACE INTO login_states VALUES(?,?)', (channel, json.dumps(body, ensure_ascii=False)))
            db.execute('INSERT INTO login_checks(body) VALUES(?)', (json.dumps(body, ensure_ascii=False),))
        return body

    def states(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            saved = {c: json.loads(b) for c, b in db.execute('SELECT channel,body FROM login_states')}
        result = {}
        for c in PLATFORMS:
            body = saved.get(c, {'channel': c, 'url': channel_url(c), 'state': 'unchecked', 'reason': '尚未检查专用 Edge 中的登录状态'})
            if body['state'] == 'authenticated' and time.time()-body.get('checked_epoch', 0) > CHECK_TTL_SECONDS:
                body = dict(body, state='stale', reason='登录检查已过期，请重新检查；浏览器中的登录信息仍保留')
            result[c] = body
        return result

    def invalidate(self, reason='浏览器会话已关闭或重启，请重新检查登录状态'):
        with closing(sqlite3.connect(self.path)) as db, db:
            for c, raw in db.execute('SELECT channel,body FROM login_states').fetchall():
                body=json.loads(raw)
                if body['state']=='authenticated':
                    body.update(state='stale', reason=reason)
                    db.execute('UPDATE login_states SET body=? WHERE channel=?', (json.dumps(body,ensure_ascii=False),c))


def classify_login(signals):
    """Search controls/content alone never prove an authenticated account."""
    evidence={k:bool(signals.get(k)) for k in ('logout_control','account_identity','private_navigation','login_control','challenge')}
    if evidence['challenge']:
        return {'state':'verification_required','reason':'页面仍要求安全验证，请在专用 Edge 中完成后检查','evidence':evidence}
    if evidence['login_control']:
        state='unknown' if evidence['logout_control'] else 'logged_out'
        return {'state':state,'reason':'仍有登录提示，尚不能确认已登录','evidence':evidence}
    if evidence['logout_control'] or (evidence['account_identity'] and evidence['private_navigation']):
        return {'state':'authenticated','reason':'当前页面存在明确的已登录账号标识','evidence':evidence}
    return {'state':'unknown','reason':'尚无明确登录标识；可展开头像菜单后检查，或本次使用公开搜索','evidence':evidence}


def prepare_plan(task, routes, states):
    selected=set(task['input']['channels']) & set(PLATFORMS)
    if set(routes)!=selected or any(v not in ('session','public') for v in routes.values()):
        raise ValueError('请为本次所选招聘平台选择已登录会话或公开搜索')
    for c, route in routes.items():
        if route=='session' and states[c]['state']!='authenticated':
            raise ValueError('所选平台尚未确认登录或检查已过期，请重新检查或选择公开搜索')
    task['login_preflight']={'version':1,'routes':routes,'prepared_at':now(),
        'checks':{c:{k:states[c].get(k) for k in ('state','checked_at','session_id')} for c in selected}}
    for channel, route in routes.items():
        if route!='session':continue
        # Rearm only login-blocked originals, and do not start a stopped/draft task here.
        from .site_flow import resume_task_site
        state=task['state'];resume_task_site(task,channel_url(channel));task['state']=state
        if not task['input'].get('onsite_search'):continue
        for city in task['input']['cities']:
            qs=task['queries']
            onsite=next((q for q in qs if q['channel']==channel and q['city']==city and q.get('kind')=='onsite' and q['status']=='pending'),None)
            if onsite is None:
                # Completed/paid queries must not be duplicated merely by reopening the dialog.
                if any(q['channel']==channel and q['city']==city and q.get('kind')=='onsite' for q in qs):continue
                text=f'{city} {task["input"]["terms"][0]} 站内搜索'
                onsite={'id':hashlib.sha256((text+channel).encode()).hexdigest()[:20],'city':city,'channel':channel,
                    'query':text,'terms':[task['input']['terms'][0]],'kind':'onsite','status':'pending'}
            else:qs.remove(onsite)
            onsite['route_reason']='已确认专用 Edge 登录；优先站内搜索，失败后按原预算回退公开索引'
            index=next((i for i,q in enumerate(qs) if q['channel']==channel and q['city']==city and q['status']=='pending'),len(qs))
            qs.insert(index,onsite)
    task['events'].append({'at':now(),'kind':'info','message':'已保存本轮账号准备与搜索方式；检查不扣搜索次数，实际搜索与降级按原预算计数'})


def query_allowed(task, query):
    return not (query.get('kind')=='onsite' and task.get('login_preflight',{}).get('routes',{}).get(query['channel'])=='public')
