"""Read-only, exact-policy-limited official link evidence, never execute external JS."""
import re
from urllib.parse import urlsplit, urljoin, parse_qsl, urlencode, urlunsplit
from .parsers import links, document
from .network import FetchError, listed_url, request_url


def canonical(url):
    p=urlsplit(url)
    return urlunsplit((p.scheme,p.netloc,p.path.rstrip('/'),urlencode(sorted(parse_qsl(p.query,keep_blank_values=True))),''))


def literal_script_links(data, base):
    text=data.decode('utf-8',errors='replace').replace('\\"','"').replace("\\'","'")
    # Only quoted literal hrefs, not JS variables, expressions, or arbitrary code.
    return [dict(url=urljoin(base,m[1]),title='静态脚本字面量链接') for m in re.finditer(r'''href\s*=\s*["']([^"'<>\s{}+\\]+)["']''',text) if not m[1].startswith(('javascript:', '#'))]


def verify_navigation(fetcher,candidate,home):
    base=home.get('final_url',candidate.official_url)
    found=links(home['data'],base)
    evidence=[]
    scripts={urljoin(base,s) for s in document(home['data']).xpath('//script/@src')}
    for url in candidate.navigation_resources:
        if url not in scripts or urlsplit(url).hostname != urlsplit(base).hostname:
            raise FetchError('official_unverified','导航资源不是官网明确引用的同域静态资源')
        page=fetcher.get(url)
        found.extend(literal_script_links(page['data'],base))
        evidence.append(dict(resource=url,hash=page['hash'],method='literal_href_no_execution'))
    for target in [*candidate.navigation_urls,candidate.career_url]:
        nav=[a for a in found if canonical(a['url'])==canonical(target)]
        if not nav:
            raise FetchError('official_unverified','原站导航未找到已声明入口：'+target)
        evidence.append(dict(target=target,links=nav))
        if target != candidate.career_url:
            page=fetcher.get(target)
            found=links(page['data'],page.get('final_url',target))
            evidence.append(dict(url=target,hash=page['hash']))
    return evidence


def verified_pagination(data, base, requested):
    observed={canonical(a['url']) for a in links(data,base)}
    for url in requested:
        if urlsplit(url).hostname!=urlsplit(base).hostname or canonical(url) not in observed:
            raise FetchError('official_unverified','分页必须是原页明示的同域链接')
    return list(dict.fromkeys(requested))


def observed_link_allowed(fetcher, page, base, target):
    """Only links actually present in this page; no permissions from page text."""
    observed = {a['url'] for a in links(page['data'], base)}
    if target not in observed:
        return False
    p, origin = urlsplit(target), urlsplit(base)
    if p.scheme != 'https' or p.username or p.password or p.port not in (None,443):
        return False
    policy = fetcher.policies.get(p.hostname, {})
    if policy.get('access') not in ('allowed_auto', 'limited_read_evaluation') or not policy.get('reviewed_at') or not policy.get('evidence_url'):
        return False
    if listed_url(target, policy.get('urls', [])):
        return True
    # Reviewed site-specific job paths, not a generic same-domain permission.
    path = p.path
    if p.hostname != origin.hostname or '%' in path or '..' in path or '\\' in path:
        return False
    prefixes = policy.get('linked_job_paths', [])
    if any(prefix.startswith('/') and prefix != '/' and '%' not in prefix and '..' not in prefix
           and (path == prefix.rstrip('/') or path.startswith(prefix.rstrip('/') + '/')) for prefix in prefixes):
        fetcher.observed_urls.add(request_url(target))
        return True
    return False


def discover_career(fetcher, candidate, home):
    base = home.get('final_url', candidate.official_url)
    options = []
    for a in links(home['data'], base):
        title_match = re.search(r'社会招聘|校园招聘|人才招聘|加入我们|招聘职位|招聘|careers|join\s+us', a['title'] or '', re.I)
        path_match = re.search(r'/(?:careers?|recruit(?:ment)?)(?:[/.]|$)', urlsplit(a['url']).path, re.I)
        # Dedicated hiring hosts often use a root URL and a creative label.
        # Recognition never grants access to an unreviewed destination.
        host_match = re.match(r'^(?:careers?|jobs?|recruit(?:ment)?|zhaopin)\.', urlsplit(a['url']).hostname or '', re.I)
        join_match = re.search(r'(?:^|[，,！!\s])加入\s*$', a['title'] or '')
        if not title_match and not path_match and not host_match and not join_match:
            continue
        if canonical(a['url']) == canonical(base):
            continue
        if not any(x['url'] == a['url'] for x in options):
            options.append(a | {'approved': observed_link_allowed(fetcher, home, base, a['url'])})
    # Prefer social hiring; do not silently invent job-type constraints.
    options.sort(key=lambda a: (not a['approved'], '社会招聘' not in a['title']))
    return options


def job_links(page, base):
    output=[]
    for a in links(page['data'], base):
        if canonical(a['url']) == canonical(base):
            continue
        path=urlsplit(a['url']).path
        if re.search(r'/(?:jobs?|careers?|positions?|recruit(?:ment)?|socialrecruit)(?:[/_.-]|$)|job[_-](?:list|detail)', path, re.I) and not re.search(r'apply|submit|login|logout|signin|delete',path,re.I):
            if not any(x['url']==a['url'] for x in output):
                output.append(a)
    return output
