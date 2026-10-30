"""Bounded public GET. No credentials, browser automation, proxy overrides or TLS bypass."""
from __future__ import annotations
import hashlib
import ipaddress
import re
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import http.cookiejar
import http.client
import json
from pathlib import Path
from .models import Budget, now
from .robots_cache import RobotsCache


class FetchError(Exception):
    def __init__(self, status, message, *, error_kind=None, retryable=None, cause_status=None):
        self.status = status
        self.error_kind = error_kind
        self.retryable = status == 'failed' if retryable is None else retryable
        self.cause_status = cause_status
        super().__init__(message)


def classify_transport_error(exc):
    """Describe observed local errors, without guessing the remote server's cause."""
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    message = str(reason)
    upper = message.upper()
    certificate = isinstance(reason, ssl.SSLCertVerificationError) or 'CERTIFICATE_VERIFY_FAILED' in upper
    if certificate:
        code = getattr(reason, 'verify_code', None)
        kind = 'certificate_expired' if code == 10 or 'CERTIFICATE HAS EXPIRED' in upper else 'certificate_verification_failed'
        return dict(status='tls_error', error_kind=kind, retryable=False,
                    tls_verify_code=code, tls_verify_message=getattr(reason, 'verify_message', None))
    if 'HANDSHAKE OPERATION TIMED OUT' in upper:
        kind = 'tls_handshake_timeout'
    elif isinstance(reason, ssl.SSLEOFError) or 'UNEXPECTED_EOF_WHILE_READING' in upper:
        kind = 'tls_eof'
    elif 'HANDSHAKE_FAILURE' in upper:
        kind = 'tls_handshake_failure'
    elif isinstance(reason, ssl.SSLError):
        kind = 'tls_protocol_error'
    elif isinstance(reason, (TimeoutError, socket.timeout)) or 'TIMED OUT' in upper:
        kind = 'read_timeout' if 'READ OPERATION' in upper else 'network_timeout'
    elif isinstance(reason, socket.gaierror):
        kind = 'dns_resolution_failed'
    elif isinstance(reason, ConnectionResetError):
        kind = 'connection_reset'
    else:
        kind = 'transport_error'
    return dict(status='failed', error_kind=kind, retryable=True)


def parse_robots(data, content_type):
    """Reject non-rule responses instead of interpreting an HTML error as allow-all."""
    mime = (content_type or '').split(';', 1)[0].strip().lower()
    if mime and mime != 'text/plain':
        raise FetchError('robots_unavailable', 'robots 响应不是 text/plain：'+mime,
                         error_kind='robots_content_type', retryable=False)
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise FetchError('robots_unavailable', 'robots 不是有效 UTF-8 文本',
                         error_kind='robots_encoding', retryable=False) from exc
    lines = text.splitlines()
    meaningful = [line.split('#', 1)[0].strip() for line in lines if line.split('#', 1)[0].strip()]
    if '\x00' in text or any(re.search(r'<\s*!?/?[a-z]', line, re.I) for line in meaningful):
        raise FetchError('robots_unavailable', 'robots 返回 HTML 或非规则内容',
                         error_kind='robots_invalid_content', retryable=False)
    # Empty/comment-only text/plain is a valid empty file. Missing MIME requires
    # actual rule syntax; malformed nonempty responses never grant permission.
    directives = [line.split(':', 1)[0].strip().lower() for line in meaningful if ':' in line]
    if (meaningful and ('user-agent' not in directives or len(directives) != len(meaningful))) or (not mime and not meaningful):
        raise FetchError('robots_unavailable', 'robots 未取得可识别的规则文本',
                         error_kind='robots_invalid_content', retryable=False)
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(lines)
    return rp


def request_url(url):
    """HTTP identity only: preserve path/query; fragments remain in discovery records."""
    return urllib.parse.urldefrag(url)[0]


def listed_url(url, urls):
    return request_url(url) in {request_url(x) for x in urls}


def redirect_target(source, location, policies):
    target = request_url(urllib.parse.urljoin(source, location or ""))
    old, new = urllib.parse.urlsplit(source), urllib.parse.urlsplit(target)
    if new.scheme != "https" or new.username or new.password or new.port not in (None, 443):
        raise FetchError("blocked", "重定向协议/凭据/端口不安全")
    try:
        if not ipaddress.ip_address(new.hostname).is_global:
            raise FetchError("blocked", "拒绝内网/回环跳转")
    except ValueError:
        pass
    policy = policies.get(new.hostname, {})
    if not listed_url(target, policy.get("urls", [])):
        raise FetchError("redirect_pending", "跳转目标不在逐URL审核范围："+target)
    pairs = {(request_url(a), request_url(b)) for a,b in policies.get(old.hostname, {}).get("approved_redirects", [])}
    if old.hostname != new.hostname and (request_url(source),target) not in pairs:
        raise FetchError("redirect_pending", "跨域关联未审核："+target)
    return target


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Fetcher:
    def __init__(self, budget: Budget, policies: dict, evidence_dir: Path, *, robots_cache=None):
        self.budget = budget
        self.policies = policies
        self.evidence_dir = evidence_dir
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.requests = 0
        self.last = {}
        self.stopped_hosts = {}
        self.robots = {}
        self.robots_failures = {}  # One bounded verification per host in this Fetcher.
        self.robots_checks = []
        self.robots_cache = robots_cache if robots_cache is not None else RobotsCache()
        self.robots_evidence = {}
        self.required_intervals = {}
        self.cache = {}
        self.events = []
        self.read_attempts = []  # Includes failures before any HTTP request was sent.
        self.observed_urls = set()
        # Fresh anonymous cookies only. Never load browser sessions or persist credentials.
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies), NoRedirect())

    def policy(self, url):
        p = urllib.parse.urlsplit(url)
        if p.scheme != "https" or p.username or p.password or p.port not in (None, 443):
            raise FetchError("blocked", "仅允许无凭据的 HTTPS 公网 URL")
        policy = self.policies.get(p.hostname)
        if not policy or policy.get("access") not in ("allowed_auto", "limited_read_evaluation"):
            raise FetchError("policy_pending", "站点访问策略尚未批准/仅可人工导入")
        if not policy.get("reviewed_at") or not policy.get("evidence_url"):
            raise FetchError("policy_pending", "缺少策略审核来源与时间")
        if not listed_url(url, policy.get("urls", [])) and request_url(url) not in self.observed_urls and p.path != "/robots.txt":
            raise FetchError("blocked", "URL 不在本次明确审核的有限读取范围")
        try:
            addresses = socket.getaddrinfo(p.hostname, 443, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise FetchError("failed", "DNS解析失败："+str(exc), error_kind='dns_resolution_failed') from exc
        for entry in addresses:
            if not ipaddress.ip_address(entry[4][0]).is_global:
                raise FetchError("blocked", "禁止访问内网/回环地址")
        return p, policy

    def _request(self, url, body=None, *, purpose=None):
        url = request_url(url)
        p, policy = self.policy(url)
        client = policy.get('http_client','urllib')
        deadline_seconds = policy.get('request_deadline_seconds', 60)
        if isinstance(deadline_seconds, bool) or not isinstance(deadline_seconds, (int,float)) or not 1 <= deadline_seconds <= 60:
            raise FetchError('policy_pending', '单次传输总时限须为1至60秒')
        if client not in ('urllib','windows_curl'):
            raise FetchError('policy_pending','未审核的 HTTP 客户端')
        if client == 'windows_curl':
            if body is not None or not policy.get('http_client_evidence'):
                raise FetchError('policy_pending','Schannel 仅限有对照证据的显式公开 GET 策略')
            from .curl_transport import ensure_available, open_verified_get
            try:
                ensure_available()
            except (ValueError, OSError, subprocess.SubprocessError) as exc:
                raise FetchError('policy_pending',str(exc)) from exc
        interval = max(self.budget.domain_interval, self.required_intervals.get(p.hostname, 0))
        delay = interval - (time.monotonic() - self.last.get(p.hostname, 0))
        if delay > 0:
            time.sleep(delay)
        if self.budget.requests is not None and self.requests >= self.budget.requests:
            raise FetchError("budget_exhausted", "请求预算已用尽（包含robots/导航/失败请求）")
        self.requests += 1
        self.last[p.hostname] = time.monotonic()
        self.robots_cache.touch(RobotsCache.key(p.hostname, policy))
        started = time.monotonic()
        event = {"url": url, "at": now(), "attempt": self.requests, 'method':'POST' if body is not None else 'GET',
                 'timeout_seconds':min(self.budget.timeout,deadline_seconds), 'request_purpose':purpose or ('robots' if p.path == '/robots.txt' else 'content'),
                 'http_client':client, 'domain_interval_seconds':interval, 'request_deadline_seconds':deadline_seconds}
        self.events.append(event)
        headers={"User-Agent": "JobFinder/0.2 (public read-only evaluation)", "Accept": "text/html,application/json,application/octet-stream;q=0.5"}
        if event['request_purpose'] == 'robots':
            headers['Accept'] = 'text/plain'
        if body is not None:
            headers['Content-Type']='application/json'
            event['read_request']=json.loads(body)
        request = urllib.request.Request(url, data=body, headers=headers)
        cookie_state = {(c.domain,c.path,c.name,c.value) for c in self.cookies}
        phase = 'open'
        try:
            if client == 'windows_curl':
                try:
                    response_context = open_verified_get(request,min(self.budget.timeout,deadline_seconds),self.budget.max_bytes,self.evidence_dir)
                except ValueError as exc:
                    event.update(status='failed',error_kind='client_response_invalid',retryable=False,error=str(exc))
                    raise FetchError('failed',str(exc),error_kind='client_response_invalid',retryable=False) from exc
            else:
                response_context = self.opener.open(request, timeout=min(self.budget.timeout,deadline_seconds))
            with response_context as response:
                event.update(getattr(response,'transport_details',{}))
                phase = 'read'
                event.update(http_status=response.status, content_type=response.headers.get("Content-Type"),
                             cache_control=response.headers.get('Cache-Control'), cache_age=response.headers.get('Age'),
                             sets_cookie=bool(response.headers.get('Set-Cookie')))
                data = self._read_body(response, started + deadline_seconds)
                if len(data) > self.budget.max_bytes:
                    raise FetchError("failed", "响应超过本次体积限制")
                if re.search(rb"captcha|verify you are human|access denied", data[:12000], re.I) and len(data) < 15000:
                    event['status'] = 'blocked'
                    raise FetchError("blocked", "返回验证码或访问拒绝页面，停止自动读取")
                if re.search(rb'<input[^>]+type\s*=\s*[\"\']password', data, re.I) and re.search(r'/(?:login|signin|sign-in)(?:/|$)', p.path, re.I):
                    event['status'] = 'requires_authorization'
                    raise FetchError('requires_authorization', '返回登录页；需用户在官方页面登录或提供有权使用的材料，不收集账号密码')
                event["status"] = "success"
                if event['request_purpose'] == 'robots':
                    digest = hashlib.sha256(data).hexdigest()
                    path = self.evidence_dir / (digest+'.bin')
                    if not path.exists():
                        path.write_bytes(data)
                    event.update(response_hash=digest, response_snapshot=str(path))
                return data
        except urllib.error.HTTPError as exc:
            event.update(getattr(exc,'transport_details',{}))
            exc.close()  # No error body is consumed; release the failed response.
            event.update(http_status=exc.code, content_type=exc.headers.get('Content-Type'),
                         cache_control=exc.headers.get('Cache-Control'), cache_age=exc.headers.get('Age'),
                         sets_cookie=bool(exc.headers.get('Set-Cookie')),
                         retry_after=exc.headers.get("Retry-After"), status="blocked" if exc.code in (401, 403, 418, 429) else "failed")
            if exc.code == 429:
                raise FetchError("rate_limited", f"HTTP429；Retry-After={exc.headers.get('Retry-After')}，本次停止，不提前重试") from exc
            if 300 <= exc.code < 400:
                event['status'] = 'redirect'
                event["redirect_location"] = exc.headers.get("Location")
                err = FetchError("redirect", "HTTP重定向")
                err.location = exc.headers.get("Location")
                err.session_changed = cookie_state != {(c.domain,c.path,c.name,c.value) for c in self.cookies}
                event['anonymous_session_changed'] = err.session_changed
                raise err from exc
            if exc.code == 401:
                event['status'] = 'requires_authorization'
                raise FetchError('requires_authorization', 'HTTP401：此入口需要授权，未尝试登录') from exc
            status = 'not_found' if exc.code == 404 else 'gone' if exc.code == 410 else event['status']
            event['status'] = status
            # 4xx negotiation/client failures cannot improve by repeating the
            # identical request. A 406 remains unknown, never permission to read.
            retryable = status == 'failed' and (exc.code >= 500 or exc.code == 408)
            event.update(error_kind='http_'+str(exc.code), retryable=retryable)
            raise FetchError(status, f"HTTP {exc.code}", error_kind='http_'+str(exc.code), retryable=retryable) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            classification = classify_transport_error(exc)
            event.update(**classification, error=str(exc), failure_phase=phase)
            event.update(getattr(exc,'transport_details',{}))
            raise FetchError(classification['status'], str(exc), error_kind=classification['error_kind'],
                             retryable=classification['retryable']) from exc
        finally:
            event['elapsed_seconds'] = round(time.monotonic()-started, 3)

    def _read_body(self, response, deadline):
        # urllib timeout is per blocking socket operation, not a body deadline.
        # read1 avoids waiting indefinitely for a large read while bytes trickle in.
        if not isinstance(response, http.client.HTTPResponse):
            return response.read(self.budget.max_bytes + 1)  # curl already has --max-time
        chunks, size = [], 0
        while size <= self.budget.max_bytes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('The read operation timed out: request deadline exceeded')
            sock = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
            if sock is not None:
                sock.settimeout(min(self.budget.timeout, remaining))
            chunk = response.read1(min(65536, self.budget.max_bytes + 1 - size))
            if time.monotonic() > deadline:
                raise TimeoutError('The read operation timed out: request deadline exceeded')
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        return b''.join(chunks)

    def _request_retry(self, url, body=None, *, purpose=None):
        host = urllib.parse.urlsplit(url).hostname
        if host in self.stopped_hosts:
            raise FetchError(self.stopped_hosts[host], '本轮站点已受限，停止继续请求')
        for attempt in range(self.budget.retries + 1):
            event_count = len(self.events)
            try:
                if purpose is not None:
                    return self._request(url, body, purpose=purpose)
                return self._request(url) if body is None else self._request(url,body)
            except FetchError as exc:
                if len(self.events) > event_count and exc.status in ('blocked','requires_authorization','rate_limited','tls_error'):
                    self.stopped_hosts[host] = exc.status
                if not exc.retryable or attempt == self.budget.retries:
                    raise
                time.sleep(min(2 ** attempt, 4))

    def _read_robots(self, robot_url, policy):
        """Follow only separately reviewed pairs, up to five hops, never HTML as rules."""
        current, visited = robot_url, set()
        pairs = {(request_url(a), request_url(b)) for a, b in policy.get('robots_redirects', [])}
        while True:
            visited.add(current)
            try:
                data = self._request_retry(current) if current == robot_url else self._request_retry(current, purpose='robots')
                event = next((e for e in reversed(self.events) if e['url'] == current and e.get('status') == 'success'), {})
                return data, event
            except FetchError as exc:
                if exc.status != 'redirect':
                    raise
                target = request_url(urllib.parse.urljoin(current, exc.location or ''))
                if (current, target) not in pairs:
                    raise FetchError('robots_unavailable', 'robots 跳转尚未单独审核：'+target,
                                     error_kind='robots_redirect_unverified', retryable=False) from exc
                if target in visited or len(visited) > 5:
                    raise FetchError('robots_unavailable', 'robots 跳转循环或超过5次上限',
                                     error_kind='robots_redirect_limit', retryable=False) from exc
                # Normal URL and cross-origin approval still applies. This does
                # not recursively request the target origin's robots file.
                current = redirect_target(current, exc.location, self.policies)

    @staticmethod
    def _robots_ttl(event, *, same_session=False):
        control = (event.get('cache_control') or '').lower()
        if (event.get('sets_cookie') and not same_session) or any(token in control for token in ('no-store','no-cache','private')):
            return 0
        match = re.search(r'(?:^|,)\s*max-age\s*=\s*"?(\d+)', control)
        ttl = min(3600, int(match[1])) if match else 3600
        try:
            return max(0, ttl - max(0, int(event.get('cache_age') or 0)))
        except ValueError:
            return 0

    def _restore_robots(self, host, policy, check):
        entry = self.robots_cache.get(RobotsCache.key(host, policy))
        if not entry:
            return False
        payload = entry['payload']
        try:
            if entry['kind'] == 'rules':
                data = bytes.fromhex(payload['body_hex'])
                rp = parse_robots(data, payload['event'].get('content_type'))
                digest = hashlib.sha256(data).hexdigest()
                if digest != payload['event'].get('response_hash'):
                    return False
                path = self.evidence_dir / (digest+'.bin')
                if not path.exists():
                    path.write_bytes(data)
                self.robots[host] = rp
            elif entry['kind'] == 'missing' and policy.get('robots_404_public_read') and payload['event'].get('http_status') == 404:
                self.robots[host] = None
            elif entry['kind'] == 'failure':
                if payload['status'] != 'robots_unavailable':
                    return False
                self.robots_failures[host] = FetchError(payload['status'], payload['reason'],
                    error_kind=payload['error_kind'], cause_status=payload.get('cause_status'), retryable=False)
            else:
                return False
            self.robots_evidence[host] = dict(checked_at=payload['checked_at'],
                cache_expires_at=entry['expires'], source_url=payload.get('event',{}).get('url'),
                response_hash=payload.get('event',{}).get('response_hash'))
            age = max(0, time.time() - float(entry.get('last_request_at', entry['created'])))
            self.last[host] = max(self.last.get(host, 0), time.monotonic() - age)
            check.update(cached=True, cache_source='shared_or_persistent')
            return True
        except (ValueError, KeyError, TypeError, FetchError, OSError):
            return False

    def _check_robots(self,url,p,policy):
        robot_url = f"https://{p.netloc}/robots.txt"
        check = dict(target_url=url, robots_url=robot_url, at=now(),
                     cached=p.hostname in self.robots or p.hostname in self.robots_failures)
        self.robots_checks.append(check)
        try:
            expiry = self.robots_evidence.get(p.hostname, {}).get('cache_expires_at')
            if expiry is not None and time.time() >= expiry:
                self.robots.pop(p.hostname, None)
                self.robots_failures.pop(p.hostname, None)
                self.robots_evidence.pop(p.hostname, None)
                check['cached'] = False
            if p.hostname not in self.robots and p.hostname not in self.robots_failures:
                self._restore_robots(p.hostname, policy, check)
            check.update(self.robots_evidence.get(p.hostname, {}))
            if p.hostname in self.robots_failures:
                raise self.robots_failures[p.hostname]
            if p.hostname not in self.robots:
                before = len(self.events)
                try:
                    robot_data, event = self._read_robots(robot_url, policy)
                    rp = parse_robots(robot_data, event.get('content_type'))
                    self.robots[p.hostname] = rp
                    self.robots_evidence[p.hostname] = dict(checked_at=event.get('at', now()), source_url=event.get('url',robot_url), response_hash=event.get('response_hash'), cache_expires_at=time.time()+self._robots_ttl(event,same_session=True))
                    self.robots_cache.put(RobotsCache.key(p.hostname, policy), 'rules',
                        dict(body_hex=robot_data.hex(), event=event, checked_at=event.get('at',now())), self._robots_ttl(event))
                except FetchError as exc:
                    if exc.status == "not_found" and policy.get("robots_404_public_read", False):
                        self.robots[p.hostname] = None
                        event = self.events[-1] if len(self.events) > before else {}
                        self.robots_evidence[p.hostname] = dict(checked_at=event.get('at',now()), source_url=event.get('url',robot_url), cache_expires_at=time.time()+self._robots_ttl(event,same_session=True))
                        self.robots_cache.put(RobotsCache.key(p.hostname, policy), 'missing',
                            dict(event=event, checked_at=event.get('at',now())), self._robots_ttl(event))
                    else:
                        status = 'robots_unavailable' if exc.status in ('failed', 'not_found', 'redirect') else exc.status
                        kind = exc.error_kind or ('robots_redirect_unverified' if exc.status == 'redirect' else 'robots_verification_failed')
                        failure = FetchError(status, "robots 无法核实：" + str(exc), error_kind=kind,
                                             retryable=False, cause_status=exc.status)
                        if exc.status not in ('budget_exhausted','policy_pending'):
                            self.robots_failures[p.hostname] = failure
                        if status == 'robots_unavailable':
                            self.robots_evidence[p.hostname] = dict(checked_at=now(), cache_expires_at=time.time()+60)
                            self.robots_cache.put(RobotsCache.key(p.hostname, policy), 'failure',
                                dict(status=status, reason=str(failure), error_kind=kind,
                                     cause_status=exc.status, checked_at=now()), 60)
                        raise failure from exc
            rp = self.robots[p.hostname]
            if rp is not None:
                if not rp.can_fetch("JobFinder", url):
                    raise FetchError("blocked", "robots 不允许此路径", error_kind='robots_disallowed')
                crawl_delay = rp.crawl_delay("JobFinder") or rp.crawl_delay("*")
                rate = rp.request_rate('JobFinder') or rp.request_rate('*')
                required = max(crawl_delay or 0, rate.seconds/rate.requests if rate and rate.requests else 0)
                if required > 60:
                    raise FetchError('policy_pending', 'robots要求间隔超过60秒，保留待续，避免阻塞本批其他企业', error_kind='robots_crawl_delay')
                self.required_intervals[p.hostname] = max(self.required_intervals.get(p.hostname,0), required)
                check['effective_interval_seconds'] = max(self.budget.domain_interval, required)
            check.update(self.robots_evidence.get(p.hostname, {}))
            check.update(decision='allowed', reason='reviewed_404_public_read' if rp is None else 'valid_rules_allow')
        except FetchError as exc:
            check.update(decision='disallowed' if exc.error_kind == 'robots_disallowed' else 'unknown',
                         status=exc.status, error_kind=exc.error_kind, cause_status=exc.cause_status, reason=str(exc))
            raise
    def _track_read(self, url, method, read):
        before = self.requests
        robots_before = len(self.robots_checks)
        attempt = dict(url=request_url(url), method=method, started_at=now(), status='started')
        self.read_attempts.append(attempt)
        try:
            result = read()
            attempt.update(status='success', content_hash=result.get('hash'),
                           retrieved_at=result.get('retrieved_at'))
            return result
        except FetchError as exc:
            attempt.update(status=exc.status, reason=str(exc), error_kind=exc.error_kind, cause_status=exc.cause_status)
            raise
        finally:
            attempt.update(finished_at=now(), http_requests=self.requests-before)
            attempt['failed_before_http'] = attempt['status'] != 'success' and self.requests == before
            check = next((c for c in reversed(self.robots_checks[robots_before:])
                          if request_url(c['target_url']) == request_url(url)), None)
            if check is not None:
                attempt['robots_check'] = dict(check)

    def read_moka_job(self,url,payload):
        return self._track_read(url, 'public_job_post', lambda: self._read_moka_job(url,payload))

    def _read_moka_job(self,url,payload):
        """Only the reviewed public read endpoint; no arbitrary POST or credentials."""
        p,policy=self.policy(url)
        contract=policy.get('moka_public_job_read',{})
        if p.path!='/api/outer/ats-apply/website/job' or p.query or contract.get('url')!=url or not contract.get('evidence_url'):
            raise FetchError('policy_pending','公开岗位详情读取契约未审核')
        site=str(payload.get('siteId',''))
        if set(payload)!={'orgId','siteId','jobId','locale'} or payload.get('orgId')!=contract.get('orgId') or not site.isdigit() or site!=str(contract.get('siteId')) or payload.get('locale')!='zh-CN' or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',str(payload.get('jobId',''))):
            raise FetchError('blocked','拒绝契约外的读取参数')
        self._check_robots(url,p,policy)
        body=json.dumps(payload,separators=(',',':')).encode()
        key=url+'#request-sha256='+hashlib.sha256(body).hexdigest()
        if key in self.cache:
            return self.cache[key]
        try:
            data=self._request_retry(url,body)
        except FetchError as exc:
            if exc.status=='redirect':
                raise FetchError('redirect_pending','只读POST发生跳转，需要重新审核；不重放请求') from exc
            raise
        digest=hashlib.sha256(data).hexdigest()
        path=self.evidence_dir/(digest+'.bin')
        if not path.exists():
            path.write_bytes(data)
        result=dict(data=data,hash=digest,snapshot=str(path),retrieved_at=now(),request=payload,source_url=url)
        self.cache[key]=result
        return result

    def get(self, url, check_robots=True, _chain=(), _session_retry=False):
        return self._track_read(url, 'public_get', lambda: self._get(url,check_robots,_chain,_session_retry))

    def _get(self, url, check_robots=True, _chain=(), _session_retry=False):
        url = request_url(url)
        _chain = tuple(request_url(x) for x in _chain)
        if url in _chain or len(_chain) > 3:
            raise FetchError("blocked", "跳转循环或超过3次上限")
        if url in self.cache:
            return self.cache[url]
        p, policy = self.policy(url)
        if check_robots:
            self._check_robots(url,p,policy)
        try:
            data = self._request_retry(url)
            digest = hashlib.sha256(data).hexdigest()
            path = self.evidence_dir / (digest + ".bin")
            if not path.exists():
                path.write_bytes(data)
            result = {"data": data, "hash": digest, "snapshot": str(path), "retrieved_at": now()}
            self.cache[url] = result
            return result
        except FetchError as exc:
            if exc.status == "redirect":
                target = redirect_target(url, exc.location, self.policies)
                if target == url and getattr(exc, 'session_changed', False) and not _session_retry:
                    result = self.get(target, check_robots=True, _chain=_chain, _session_retry=True)
                else:
                    result = self.get(target, check_robots=True, _chain=(*_chain,url))
                result = result | {"redirect_chain":[*_chain,url,target], "final_url":result.get("final_url",target)}
                self.cache[url] = result
                return result
            raise
