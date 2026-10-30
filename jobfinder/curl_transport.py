"""Explicit Windows Schannel GET transport; no automatic fallback or policy bypass."""
from __future__ import annotations

import http.client
import io
import json
import re
import ssl
import subprocess
import tempfile
import urllib.error
import urllib.parse
from functools import lru_cache
from pathlib import Path

CURL = Path('C:/Windows/System32/curl.exe')


@lru_cache(maxsize=1)
def ensure_available():
    if not CURL.is_file():
        raise ValueError('已审核的系统 curl.exe 不可用；不安装或切换其他客户端')
    result = subprocess.run([str(CURL), '--disable', '--version'], capture_output=True,
                            text=True, encoding='utf-8', errors='replace', timeout=5)
    match = re.search(r'^curl (\d+)\.', result.stdout)
    if result.returncode or not match or int(match[1]) < 8 or 'Schannel' not in result.stdout:
        raise ValueError('需现有 curl 8+ / Schannel；未找到经验证的客户端')
    return result.stdout.splitlines()[0]


class Response:
    def __init__(self, data, status, headers, details):
        self.data, self.status, self.headers, self.transport_details = data, status, headers, details
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self, limit): return self.data[:limit]


def response_headers(raw, status):
    blocks = re.split(rb'\r?\n\r?\n', raw)
    blocks = [block for block in blocks if re.match(rb'^HTTP/\S+\s+\d{3}\b', block)]
    if not blocks or int(blocks[-1].split(None, 2)[1]) != status:
        raise ValueError('curl 未取得与最终 HTTP 状态一致的响应头')
    lines = blocks[-1].splitlines(keepends=True)
    return http.client.parse_headers(io.BytesIO(b''.join(lines[1:])+b'\r\n\r\n'))


def open_verified_get(request, timeout, max_bytes, evidence_dir):
    """Return one anonymous response. Fetcher owns DNS/policy/robots/rate/retry."""
    ensure_available()
    if request.get_method() != 'GET' or request.data is not None:
        raise ValueError('Schannel 客户端仅接受已审核公开 GET，不接收 POST 或凭据')
    url = request.full_url
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None,443):
        raise ValueError('Schannel GET 仅接受无凭据的标准 HTTPS URL')
    # Only these core-created headers are accepted; never forward login material.
    headers = {key.lower():value for key,value in request.header_items()}
    if set(headers) - {'user-agent','accept','connection'} or any('\r' in v or '\n' in v for v in headers.values()):
        raise ValueError('Schannel GET 不允许额外请求头')
    with tempfile.TemporaryDirectory(prefix='curl-read-', dir=evidence_dir) as tmp:
        body, head = Path(tmp)/'body', Path(tmp)/'headers'
        args = [str(CURL), '--disable', '--silent', '--show-error', '--http1.1',
                '--proto', '=https', '--tlsv1.2', '--connect-timeout', str(timeout),
                '--max-time', str(timeout), '--max-redirs', '0', '--retry', '0',
                '--max-filesize', str(max_bytes), '--output', str(body),
                '--dump-header', str(head), '--write-out', '%{json}']
        for key,value in headers.items():
            args.extend(['--header', key+': '+value])
        args.extend(['--header','Connection: close',url])
        details = {'http_client':'windows_curl','tls_implementation':'Schannel',
                   'tls_minimum':'TLSv1.2','tls_verification':True}
        try:
            result = subprocess.run(args, capture_output=True, text=True, encoding='utf-8',
                                    errors='replace', timeout=timeout+5)
        except subprocess.TimeoutExpired as exc:
            error = urllib.error.URLError(TimeoutError('系统 curl 超过命令时限，已终止'))
            error.transport_details = details | {'failure_phase':'open','curl_process_timeout':True}
            raise error from exc
        try:
            info = json.loads(result.stdout)
        except (ValueError, TypeError) as exc:
            raise ValueError('系统 curl 未返回有效的请求计量；不接受未知响应') from exc
        details.update({('curl_'+key):info.get(key) for key in
                        ['exitcode','ssl_verify_result','proxy_used','http_connect','time_namelookup',
                         'time_connect','time_appconnect','time_starttransfer','time_total']})
        status = int(info.get('response_code') or 0)
        message = info.get('errormsg') or result.stderr.strip() or 'curl transport failure'
        if result.returncode:
            if result.returncode in (51,58,60,77,83,90,91) or re.search(r'CERT_EXPIRED|UNTRUSTED_ROOT|REVOCATION',message,re.I):
                reason = ssl.SSLCertVerificationError(1,message)
            elif result.returncode == 35:
                reason = ssl.SSLError(1,message)
            elif result.returncode == 28:
                reason = TimeoutError(('The read operation timed out: ' if info.get('time_appconnect',0)>0 else 'Connection timed out: ')+message)
            else:
                reason = OSError('curl '+str(result.returncode)+': '+message)
            error = urllib.error.URLError(reason)
            error.transport_details = details | {'failure_phase':'read' if info.get('time_appconnect',0)>0 else 'open'}
            raise error
        if info.get('ssl_verify_result') != 0:
            error = urllib.error.URLError(ssl.SSLCertVerificationError(1,'curl TLS verification not successful'))
            error.transport_details = details
            raise error
        if info.get('num_redirects') != 0 or info.get('url_effective') != url or not 100 <= status <= 599:
            raise ValueError('系统 curl 返回意外的重定向/URL/状态；未接受响应')
        if not head.exists() or head.stat().st_size > 65536:
            raise ValueError('系统 curl 响应头缺失或过大')
        response_header = response_headers(head.read_bytes(),status)
        if status >= 300:
            error = urllib.error.HTTPError(url,status,'public GET',response_header,None)
            error.transport_details = details
            raise error
        if not body.exists() or body.stat().st_size > max_bytes:
            raise ValueError('系统 curl 响应正文缺失或超过限制')
        return Response(body.read_bytes(),status,response_header,details)
