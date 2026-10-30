"""Shared, persistent site gates and request pacing; never stores session data."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
import json
import math
import sqlite3
import time
from urllib.parse import urlsplit

from .recovery import PageIssue


SESSION_DOMAINS = ('zhipin.com', 'zhaopin.com', 'liepin.com', '51job.com')
GROUPED_DOMAINS = SESSION_DOMAINS + ('bing.com',)
CHANNEL_URLS = {
    'boss': 'https://www.zhipin.com/', 'zhaopin': 'https://www.zhaopin.com/',
    'liepin': 'https://www.liepin.com/', '51job': 'https://www.51job.com/',
    'bing': 'https://www.bing.com/',
}


def _host(url):
    return (urlsplit(url).hostname or '').lower().rstrip('.')


def site_key(url):
    host = _host(url)
    return next((d for d in GROUPED_DOMAINS if host == d or host.endswith('.' + d)), host)


def requires_session(url):
    return site_key(url) in SESSION_DOMAINS


def channel_url(channel):
    return CHANNEL_URLS.get(channel, '')


def parse_retry_after(value, now=None):
    """Return a UTC Unix deadline from Retry-After delta seconds or HTTP date."""
    current = time.time() if now is None else now
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    try:
        seconds = float(text)
        if math.isfinite(seconds) and seconds >= 0:
            return current + seconds
        return None
    except ValueError:
        try:
            dt = parsedate_to_datetime(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(current, dt.timestamp())
        except (ValueError, TypeError, OverflowError):
            return None


@dataclass(frozen=True)
class SiteAccessPolicy:
    min_interval: float = 3.0
    rate_limit_cooldown: float = 60.0
    policy_cooldown: float = 300.0
    max_rate_limits: int = 2

    def __post_init__(self):
        if any(not math.isfinite(v) or v < 0 for v in
               (self.min_interval, self.rate_limit_cooldown, self.policy_cooldown)):
            raise ValueError('站点等待间隔须为非负有限秒数')
        if self.max_rate_limits < 1:
            raise ValueError('站点限流次数须至少为1')


class SiteAccess:
    site_key = staticmethod(site_key)
    requires_session = staticmethod(requires_session)
    channel_url = staticmethod(channel_url)

    def __init__(self, folder, policy=None):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.db = self.folder / 'site-access.sqlite3'
        self.policy = SiteAccessPolicy(**policy) if isinstance(policy, dict) else policy or SiteAccessPolicy()
        self.total_wait_seconds = 0.0
        with self._connect() as db:
            db.executescript('''PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS sites (
                    site TEXT PRIMARY KEY, body TEXT NOT NULL,
                    last_request_at REAL NOT NULL DEFAULT 0,
                    min_interval REAL NOT NULL DEFAULT 0);
            ''')

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.db, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _empty(self, url):
        return dict(site=site_key(url), state='available', reason='', kind='', retry_at=0,
                    url=url, updated_at=0, rate_limit_count=0)

    def _effective(self, body):
        body = dict(body)
        if body['state'] == 'cooldown' and body['retry_at'] <= time.time():
            body['state'] = 'available'
        return body

    def _read(self, db, url):
        row = db.execute('SELECT * FROM sites WHERE site=?', (site_key(url),)).fetchone()
        return row, json.loads(row['body']) if row else self._empty(url)

    def _save(self, db, body):
        db.execute('INSERT INTO sites (site,body) VALUES (?,?) ON CONFLICT(site) DO UPDATE SET body=excluded.body',
                   (body['site'], json.dumps(body, ensure_ascii=False)))

    def state(self, url):
        with self._connect() as db:
            _, body = self._read(db, url)
        return self._effective(body)

    def snapshot(self):
        with self._connect() as db:
            return {r['site']: self._effective(json.loads(r['body'])) for r in db.execute('SELECT site,body FROM sites')}

    @staticmethod
    def _gate(body, manual=False):
        if body['state'] == 'available':
            return
        if manual and body['state'] == 'needs_help' and body['kind'] in ('captcha', 'login_required'):
            return
        kind = 'site_wait' if body['state'] == 'cooldown' else 'site_paused'
        raise PageIssue(body['reason'] or '该站点已暂停自动访问', kind, evidence=dict(body))

    def check(self, url, manual=False):
        """Read-only scheduling check. Does not consume a request or pacing slot."""
        body = self.state(url)
        self._gate(body, manual)
        return body

    def before_request(self, url, manual=False):
        # Never hold SQLite or Store locks while waiting. Independent instances
        # atomically claim the next request only after the shared gap elapses.
        clock_started = time.time()
        monotonic_started = time.monotonic()
        while True:
            with self._connect() as db:
                db.execute('BEGIN IMMEDIATE')
                row, body = self._read(db, url)
                effective = self._effective(body)
                self._gate(effective, manual)
                # Continue advancing during a system-clock adjustment (or a
                # frozen wall clock in an offline test); do not sleep forever.
                current = max(time.time(), clock_started + max(0, time.monotonic() - monotonic_started))
                interval = max(self.policy.min_interval, row['min_interval'] if row else 0)
                wait = max(0, row['last_request_at'] + interval - current) if row else 0
                if not wait:
                    self._save(db, body)
                    db.execute('UPDATE sites SET last_request_at=? WHERE site=?', (current, body['site']))
                    return effective
            waiting_started = time.monotonic()
            # Windows timers can report no advancement for a sub-millisecond
            # remainder. Round UP the wait; never treat that rounding as failure.
            time.sleep(max(wait, 0.02))
            elapsed = max(0, time.monotonic() - waiting_started)
            self.total_wait_seconds += elapsed
            if elapsed == 0 and time.time() <= current:
                # A non-advancing injected clock cannot attest that the gap
                # elapsed. Yield a gate instead of busy-looping or bypassing it.
                raise PageIssue('该站点尚在等待读取间隔', 'site_wait',
                                evidence=dict(effective, state='cooldown', retry_at=current + wait))

    def set_min_interval(self, url, seconds):
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError('网站读取间隔无效')
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            _, body = self._read(db, url)
            self._save(db, body)
            db.execute('UPDATE sites SET min_interval=MAX(min_interval,?) WHERE site=?', (seconds, body['site']))

    def record_issue(self, url, exc):
        if getattr(exc, '_site_recorded', False):
            return self.state(url)
        kind = getattr(exc, 'kind', '')
        if kind == 'site_wait':
            with self._connect() as db:
                db.execute('BEGIN IMMEDIATE')
                _, body = self._read(db, url)
                effective = self._effective(body)
                if effective['state']=='available':
                    retry_at=max(time.time()+0.05, (getattr(exc,'evidence',{}) or {}).get('retry_at',0))
                    body.update(state='cooldown',kind='site_wait',reason=str(exc),retry_at=retry_at,updated_at=time.time())
                    self._save(db,body)
            exc._site_recorded=True
            return self.state(url)
        if kind not in ('captcha', 'login_required', 'access_denied', 'rate_limit', 'policy_denied', 'policy_unknown'):
            return self.state(url)
        evidence = getattr(exc, 'evidence', {}) or {}
        current = time.time()
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            _, body = self._read(db, url)
            # A later generic failure cannot weaken a hard access/policy gate.
            if body['state'] == 'blocked':
                exc._site_recorded = True
                return self._effective(body)
            body.update(url=url, kind=kind, reason=str(exc)[:400], updated_at=current, retry_at=0)
            if kind in ('captcha', 'login_required'):
                body['state'] = 'needs_help'
            elif kind in ('access_denied', 'policy_denied'):
                body['state'] = 'blocked'
            elif kind == 'policy_unknown':
                body.update(state='cooldown', retry_at=current + self.policy.policy_cooldown)
            else:
                body['rate_limit_count'] += 1
                retry_at = evidence.get('retry_at')
                if not isinstance(retry_at, (int, float)) or not math.isfinite(retry_at):
                    retry_at = parse_retry_after(evidence.get('retry_after'), current)
                if retry_at is None:
                    retry_at = current + self.policy.rate_limit_cooldown
                body.update(state='blocked' if body['rate_limit_count'] >= self.policy.max_rate_limits else 'cooldown',
                            retry_at=max(current, retry_at))
            self._save(db, body)
            exc._site_recorded = True
            return self._effective(body)

    def record_success(self, url):
        """A real automatic read may close an expired rate-limit episode only."""
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            _, body = self._read(db, url)
            current = time.time()
            recovered_rate = (body['kind'] == 'rate_limit' and body['state'] == 'cooldown'
                              and body['retry_at'] <= current)
            if not recovered_rate and body['state'] != 'available':
                return self._effective(body)
            if recovered_rate:
                body.update(state='available', kind='', reason='', retry_at=0, rate_limit_count=0)
            body.update(url=url, updated_at=current, last_success_at=current)
            self._save(db, body)
            return self._effective(body)

    def release(self, url):
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            _, body = self._read(db, url)
            if body['state'] == 'needs_help' and body['kind'] in ('captcha', 'login_required'):
                body.update(state='available', reason='', kind='', retry_at=0, updated_at=time.time())
                self._save(db, body)
            elif self._effective(body)['state'] != 'available':
                self._gate(body)
            return self._effective(body)
