"""An opt-in, isolated local Edge session. Never attach to the user's daily profile."""
import hashlib
import re
import time
import base64
from urllib.parse import urlsplit, parse_qs, urlencode
from jobfinder.models import now
from .providers import AccessIssue, validate_url
from .search_routes import clean_sources
from .recovery import gate_kind, PageIssue
from .page_signals import page_readiness, is_job_detail_url


SESSION_DOMAINS=('zhaopin.com','liepin.com','51job.com','zhipin.com')


def browser_site(url):
    host=(urlsplit(url).hostname or '').lower()
    for domain in SESSION_DOMAINS+('bing.com',):
        if host==domain or host.endswith('.'+domain):return domain
    return re.sub(r'^(?:www|m|wap)\.', '',host)


def same_site_redirect(source,target):
    from .site_access import requires_session, site_key
    same = browser_site(source)==browser_site(target) or (requires_session(source) and site_key(source)==site_key(target))
    return (urlsplit(target).scheme=='https' and not urlsplit(target).username
        and not urlsplit(target).password and same)


class SearchNeedsHelp(AccessIssue):
    def __init__(self, message, receipt=None, kind='search_parse'):
        super().__init__(message,kind)
        self.receipt = receipt or {}


def bing_target(url):
    """Decode only the destination already embedded in an observed result href."""
    try:
        parts=urlsplit(url)
        if parts.hostname in ('www.bing.com','cn.bing.com') and parts.path=='/ck/a':
            encoded=parse_qs(parts.query).get('u',[''])[0]
            if encoded.startswith('a1'):
                encoded=encoded[2:]
                return base64.urlsafe_b64decode(encoded+'='*((-len(encoded))%4)).decode('utf-8')
        return url
    except (ValueError,UnicodeError):
        return ''


def parse_bing_results(observation):
    text=observation.get('text','')
    url=observation.get('url','')
    parsed=urlsplit(url)
    proof={'kind':'browser_visible_search_results','url':url,'title':observation.get('title',''),
           'retrieved_at':now(),'text':text,'content_hash':hashlib.sha256(text.encode()).hexdigest()}
    rows=observation.get('rows',[])
    if parsed.hostname not in ('cn.bing.com','www.bing.com') or parsed.path!='/search':
        raise SearchNeedsHelp('搜索页面发生跳转，请在浏览器工作区检查后重试',proof)
    if re.search(r'验证您是人类|确认您是人类|安全验证|请输入验证码|verify you are human|unusual traffic|solve.{0,20}(?:puzzle|challenge)',text,re.I):
        raise SearchNeedsHelp('搜索页需要验证，请在专用 Edge 窗口处理后点击重试；已保留本次用量',proof,'captcha')
    decoded=[dict(row,url=bing_target(row.get('url',''))) for row in rows if isinstance(row,dict)]
    results=[r for r in clean_sources(decoded) if (urlsplit(r['url']).hostname or '').lower() not in ('www.bing.com','cn.bing.com')]
    if not results and not re.search(r'没有找到与|找不到与|没有与.{0,100}相关的结果|there are no results for|no results found',text,re.I):
        raise SearchNeedsHelp('搜索页尚未读到可核验的结果，可能仍在加载、需要登录或页面结构变化；请在专用 Edge 检查后重试',proof)
    return {'results':results,'usage':{},'method':'local_edge_bing','proof':proof}


class Browser:
    def __init__(self, folder, access=None):
        self.folder = folder
        self.engine = None
        self.context = None
        self.search_page = None
        self.search_checkpoint = None
        self.access = access
        self.verification_pages = {}
        self.verification_urls = {}
        self.checkpoints = {}
        self.manual_pages = set()
        self.session_id = None

    def check_login(self, channel):
        """Inspect this profile's visible account controls without submitting a search."""
        from .login import LoginRegistry, PLATFORMS, classify_login
        from .site_access import channel_url, site_key
        if channel not in PLATFORMS:raise ValueError('未支持的登录平台')
        registry=LoginRegistry(self.folder);url=channel_url(channel)
        try:
            if self.access:self.access.check(url,manual=True)
            self.launch()
            page=self.verification_page(url)
            if page is None:
                page=next((p for p in self.context.pages if not p.is_closed() and site_key(p.url)==site_key(url)),None)
            if page is None:
                self.open(url);page=self.verification_page(url)
            if page is None or not same_site_redirect(url,page.url):
                return registry.save(channel,{'state':'unknown','reason':'尚未找到同平台的登录页面，请打开专用 Edge 登录入口'},self.session_id)
            self.retain(page,url)
            signals=page.evaluate('''() => {
              const visible=e=>!!e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden';
              const controls=[...document.querySelectorAll('a,button,[role="button"],input[type="submit"]')].filter(visible);
              const labels=controls.map(e=>(e.innerText||e.value||'').trim());
              const text=document.body?.innerText||'';
              const identity=[...document.querySelectorAll('.user-name,.username,.user-nickname,.nick-name,[data-testid="user-name"]')]
                .filter(visible).some(e=>{const t=e.innerText.trim();return t.length>0&&t.length<60&&!/登录|注册|游客|求职者|招聘者|个人中心|我的账号/.test(t)});
              return {logout_control:labels.some(t=>/^(退出登录|安全退出|退出账号|注销登录|log out|sign out)$/i.test(t)),
                account_identity:identity,
                private_navigation:labels.some(t=>/^(我的简历|我的投递|投递记录|我的沟通|账号设置|账号安全|账户设置)$/.test(t)),
                login_control:labels.some(t=>/^(登录|立即登录|登录[\\s/／]*注册|注册[\\s/／]*登录|sign in|log in)$/i.test(t)),
                challenge:/安全验证|请输入验证码|拖动滑块|验证您是人类|verify you are human/i.test(text)};
            }''')
            for frame in page.frames:
                if frame==page.main_frame:continue
                try:
                    if gate_kind(frame.locator('body').inner_text(timeout=300),frame.url)=='captcha':signals['challenge']=True
                except Exception:pass
            result=classify_login(signals)
            if result['state']=='authenticated' and self.access:
                gate=self.access.state(url)
                if gate['state']=='needs_help':self.access.release(url)
            return registry.save(channel,result,self.session_id)
        except (AccessIssue,PageIssue) as exc:
            state={'captcha':'verification_required','login_required':'logged_out','site_wait':'restricted','site_paused':'restricted','access_denied':'restricted','rate_limit':'restricted'}.get(exc.kind,'unknown')
            reason='该站点仍受访问限制，请先处理网站状态' if state=='restricted' else '登录检查未通过，请在专用 Edge 中完成登录或验证后重试'
            return registry.save(channel,{'state':state,'reason':reason},self.session_id)
        except Exception:
            return registry.save(channel,{'state':'unknown','reason':'无法读取登录状态，请确认专用 Edge 已打开且页面加载完成'},self.session_id)

    def resume_site(self,url):
        """Inspect a retained DOM only; the caller owns explicit gate release."""
        page=self.verification_page(url)
        result={'verified':False,'ready':False,'site':browser_site(url),'requested_url':url}
        if page is None:return result|{'status':'page_missing','reason':'验证页已关闭，请重新打开原入口','kind':'page_missing'}
        result['url']=page.url
        if not same_site_redirect(url,page.url):
            return result|{'status':'unsafe_redirect','reason':'验证页已跳转到其他站点，需核验地址','kind':'unsafe_redirect'}
        try:
            from .onsite import observe
            obs=observe(page)
            # A visible challenge may live in an embedded frame while the
            # underlying main page still exposes its search input.
            frame_texts=[]
            for frame in page.frames:
                if frame==page.main_frame:continue
                try:frame_texts.append(frame.locator('body').inner_text(timeout=300))
                except Exception:continue
            obs['text']=' '.join([obs.get('text',''),*frame_texts])
            kind=gate_kind(obs.get('text',''),page.url)
            if kind:return result|{'status':'still_gated','kind':kind,'reason':'页面仍显示登录或验证要求'}
            readiness=page_readiness(obs)
            controls=any(i.get('visible') and i.get('type') not in ('password','tel','hidden')
                and re.search('职位|公司|关键词|搜索',i.get('placeholder','')) for i in obs.get('inputs',[]))
            verified=readiness['state'] in ('job_content','empty_results') or controls
            return result|{'verified':verified,'ready':verified,'status':'ready' if verified else 'unreadable',
                'reason':'页面已显示可读岗位内容或岗位搜索控件' if verified else '尚未读到岗位内容或岗位搜索控件',
                'evidence':{'title':obs.get('title',''),'readiness':readiness,'search_controls':controls},
                'checkpoint':any(c.get('page') is page for c in self.checkpoints.values())}
        except Exception:
            return result|{'status':'unreadable','reason':'验证页仍在跳转或尚无可读内容','kind':'dynamic_unreadable'}

    def before_request(self,url,manual=False):
        if self.access:
            try:self.access.before_request(url,manual=manual)
            except PageIssue as exc:
                raise AccessIssue(str(exc),exc.kind,evidence=exc.evidence) from exc

    def record_issue(self,url,issue):
        if self.access and not getattr(issue,'_site_recorded',False):self.access.record_issue(url,issue)
        if getattr(issue,'kind','') in ('captcha','login_required'):
            from .login import LoginRegistry, channel_for
            channel=channel_for(url)
            if channel:LoginRegistry(self.folder).save(channel,{'state':'verification_required' if issue.kind=='captcha' else 'logged_out',
                'reason':'执行时发现验证要求或登录已失效，请重新检查'},self.session_id)

    def record_success(self,url):
        if self.access:self.access.record_success(url)

    def verification_page(self,url):
        site=browser_site(url);page=self.verification_pages.get(site)
        if page is not None and not page.is_closed():return page
        self.verification_pages.pop(site,None)
        self.verification_urls.pop(site,None)
        return None

    def retain(self,page,url,checkpoint=None):
        site=browser_site(url);old=self.verification_pages.get(site)
        if old is not None and old is not page and old not in self.manual_pages:
            try:old.close()
            except Exception:pass
        self.verification_pages[site]=page
        self.verification_urls[site]=url
        if checkpoint is not None:checkpoint['page']=page

    def begin_operation(self,key,url):
        self.launch()
        checkpoint=self.checkpoints.get(key)
        if checkpoint and (checkpoint.get('page') is None or checkpoint['page'].is_closed()):
            self.checkpoints.pop(key,None);checkpoint=None
        if checkpoint is None:
            saved=self.verification_page(url)
            if saved is not None and self.verification_urls.get(browser_site(url))==url:
                checkpoint={'page':saved,'requested_url':url}
        operation={'key':key,'url':url,'pages':set(),'checkpoint':checkpoint}
        def opened(page):
            try:
                if page.opener() in operation['pages']:operation['pages'].add(page)
            except Exception:pass
        operation['listener']=opened
        self.context.on('page',opened)
        if checkpoint:
            page=checkpoint['page'];operation['pages'].add(page)
        else:
            page=self.context.new_page();operation['pages'].add(page)
        operation['page']=page
        return operation

    def end_operation(self,operation,keep=False,checkpoint=None):
        page=operation['page'];key=operation['key']
        if keep:
            checkpoint=checkpoint or {'requested_url':operation['url']}
            self.retain(page,operation['url'],checkpoint)
            self.checkpoints[key]=checkpoint
        else:
            self.checkpoints.pop(key,None)
            for site,saved in list(self.verification_pages.items()):
                if saved in operation['pages']:
                    self.verification_pages.pop(site,None);self.verification_urls.pop(site,None)
        try:self.context.remove_listener('page',operation['listener'])
        except Exception:pass
        for created in operation['pages']:
            if created in self.manual_pages or (keep and created is page):continue
            try:created.close()
            except Exception:pass

    def check_gate(self,page,observation):
        kind=gate_kind(observation.get('text',''),observation.get('url',page.url))
        if kind:
            issue=AccessIssue('页面明确要求登录或验证，请在专用浏览器处理后恢复',kind,
                evidence={'url':page.url,'title':observation.get('title',''),'visible_excerpt':observation.get('text','')[:800]})
            self.record_issue(page.url,issue)
            raise issue

    def response_issue(self,response,url,page=None):
        if response is None:return
        status=response.status
        if status in (401,403,429):
            headers=getattr(response,'headers',{})
            kind='rate_limit' if status==429 else 'login_required' if status==401 else 'access_denied'
            evidence={'url':url,'status':status,'retry_after':headers.get('retry-after') if isinstance(headers,dict) else None}
            if status==403 and page is not None:
                # HTTP 403 alone proves no CAPTCHA or login flow. Read only
                # the document already present, never navigate/retry to find it.
                try:
                    obs=page.evaluate('''() => {
                      if(!document.body)return null;
                      const visible=e=>!!e.getClientRects().length;
                      const loginInput=[...document.querySelectorAll('input[type="password"],input[type="tel"]')].some(visible);
                      const loginButton=[...document.querySelectorAll('button,input[type="submit"],[role="button"]')]
                        .some(e=>visible(e)&&/^(?:登录|立即登录|登录\\s*[/／]\\s*注册|sign in|log in)$/i.test((e.innerText||e.getAttribute('value')||'').trim()));
                      return {url:location.href,text:(document.body.innerText||'').slice(0,20000),login_form:loginInput&&loginButton};
                    }''')
                    if isinstance(obs,dict) and isinstance(obs.get('text'),str) and obs.get('url','').split('#')[0]==url.split('#')[0]:
                        visible_gate=gate_kind(obs['text'])
                        if visible_gate in ('captcha','login_required'):
                            kind=visible_gate
                        elif obs.get('login_form') is True:
                            kind='login_required'
                        if kind in ('captcha','login_required'):
                            evidence.update(visible_excerpt=obs['text'][:800],classification='explicit_visible_dom')
                except Exception:pass
            message=f'浏览器返回 HTTP {status}，请在保留页面完成登录或验证' if kind in ('captcha','login_required') else f'浏览器返回 HTTP {status}，停止该站点入口'
            issue=AccessIssue(message,kind,evidence=evidence)
            if kind in ('captcha','login_required') and page is not None:self.retain(page,url)
            self.record_issue(url,issue);raise issue
        if status in (404,410):raise AccessIssue(f'浏览器返回 HTTP {status}，原招聘链接已不可用','not_found',evidence={'url':url})
        if status>=500:raise AccessIssue(f'浏览器返回 HTTP {status}，保留入口等待恢复','network_transient',evidence={'url':url})

    def search(self, query, channel=None):
        url='https://cn.bing.com/search?'+urlencode({'q':query,'setlang':'zh-hans'})
        self.before_request(url)
        self.launch()
        if self.search_page is None or self.search_page.is_closed():
            self.search_page = self.context.new_page()
            self.search_checkpoint = None
        page = self.search_page
        try:
            response=None if self.search_checkpoint==query else page.goto(url,wait_until='domcontentloaded',timeout=25000)
            self.response_issue(response,page.url,page=page)
            deadline=time.monotonic()+10
            while True:
                text=re.sub(r'\s+',' ',page.locator('body').inner_text(timeout=3000)).strip()
                rows=page.locator('#b_results .b_algo').evaluate_all("els => els.map(el => { const a=el.querySelector('h2 a[href]'); return a ? {url:a.href,title:a.innerText,summary:(el.querySelector('.b_caption p')?.innerText||'')} : null; }).filter(Boolean)")
                if rows or re.search(r'没有找到|no results|验证码|验证您|verify you|unusual traffic',text,re.I) or time.monotonic()>=deadline:
                    break
                page.wait_for_timeout(500)
            result=parse_bing_results({'url':page.url,'title':page.title(),'text':text,'rows':rows})
            self.record_success(page.url)
            self.search_checkpoint=None
            site=browser_site(url)
            if self.verification_pages.get(site) is page:
                self.verification_pages.pop(site,None);self.verification_urls.pop(site,None)
            return result
        except PageIssue as exc:
            self.record_issue(url,exc)
            if exc.kind in ('captcha','login_required'):
                self.retain(page,url);self.search_checkpoint=query
            if isinstance(exc,SearchNeedsHelp):raise
            issue=SearchNeedsHelp(str(exc),getattr(exc,'evidence',{}),exc.kind)
            issue.evidence=getattr(exc,'evidence',{})
            if getattr(exc,'_site_recorded',False):issue._site_recorded=True
            raise issue from exc
        except Exception as exc:
            raise SearchNeedsHelp('浏览器搜索连接未完成，将按有限重试规则处理',{'url':url},'network_transient') from exc

    def launch(self):
        if self.context:
            return
        try:
            from playwright.sync_api import sync_playwright
            if self.engine is None:self.engine = sync_playwright().start()
            self.context = self.engine.chromium.launch_persistent_context(str(self.folder/'browser-profile'),
                channel='msedge', headless=False, viewport={'width':1380,'height':900}, accept_downloads=False)
            import uuid
            from .login import LoginRegistry
            self.session_id=uuid.uuid4().hex
            LoginRegistry(self.folder).invalidate('专用 Edge 已重新打开，请重新检查；保存的登录会话仍在本机')
            self.context.set_default_timeout(12000)
            active_context=self.context
            def context_closed():
                if self.context is not active_context:return
                self.context=None;self.search_page=None;self.search_checkpoint=None
                LoginRegistry(self.folder).invalidate()
                self.verification_pages.clear();self.verification_urls.clear()
                self.checkpoints.clear();self.manual_pages.clear()
            self.context.on('close',context_closed)
        except Exception as exc:
            if self.engine:self.engine.stop()
            self.engine = None
            raise AccessIssue('专用 Edge 会话启动失败；请确认本机已安装 Edge，且该会话未被其他进程占用','browser_unavailable') from exc

    def onsite_search(self,channel,city,term):
        from .onsite import search
        return search(self,channel,city,term)

    def tabs(self):
        if not self.context:
            return []
        try:
            return [{'index':i,'url':p.url,'title':p.title()} for i,p in enumerate(self.context.pages) if not p.is_closed()]
        except Exception:
            self.close()
            return []

    def open(self, url):
        self.before_request(url,manual=True)
        validate_url(url)
        self.launch()
        page=self.verification_page(url)
        if page is None:
            page = self.context.new_page()
            self.manual_pages.add(page)
            response=page.goto(url, wait_until='domcontentloaded', timeout=25000)
            self.response_issue(response,page.url,page=page)
        self.manual_pages.add(page)
        # Manual reopening after a process restart also establishes the local
        # DOM handle needed by the explicit resume command; it never releases
        # the controller's persistent gate.
        self.retain(page,url)
        page.bring_to_front()
        return self.tabs()

    def read(self,url):
        """Capture the created page handle, not a tab found by exact original URL."""
        if self.access:
            try:self.access.check(url)
            except PageIssue as exc:raise AccessIssue(str(exc),exc.kind,evidence=exc.evidence) from exc
        validate_url(url)
        operation=self.begin_operation(('read',url),url)
        page=operation['page']
        redirects=[]; blocked=[];keep_open=False
        def guard(route):
            request=route.request
            if request.is_navigation_request() and request.frame==page.main_frame:
                target=request.url
                try:
                    if target!=url:
                        redirects.append({'from':url,'to':target,'at':now()})
                        if not same_site_redirect(url,target):
                            raise AccessIssue('跨站跳转已记录，暂缓读取以核验目标','unsafe_redirect',evidence={'requested_url':url,'final_url':target})
                        self.before_request(target)
                    validate_url(target)
                except ValueError as exc:
                    blocked.append(exc);route.abort();return
            route.fallback()
        page.route('**/*',guard)
        try:
            try:
                # External scripts can delay DOMContentLoaded after the main
                # document has already committed. Let capture() wait for visible
                # business content instead of discarding that loaded document.
                restore_original=False
                if operation['checkpoint']:
                    if not same_site_redirect(url,page.url):
                        raise AccessIssue('保留页已跨站跳转，请核验目标','unsafe_redirect',evidence={'requested_url':url,'final_url':page.url})
                    from .onsite import observe
                    observation=observe(page)
                    self.check_gate(page,observation)
                    controls=any(i.get('visible') and i.get('type') not in ('password','tel','hidden')
                        and re.search('职位|公司|关键词|搜索',i.get('placeholder','')) for i in observation.get('inputs',[]))
                    duties=bool(re.search('岗位职责|职位描述|工作职责|任职要求',observation.get('text','')))
                    restore_original=(page.url!=url and not is_job_detail_url(page.url)) or (controls and not duties)
                if operation['checkpoint'] and not restore_original:
                    response=None
                else:
                    self.before_request(url)
                    response=page.goto(url,wait_until='commit',timeout=25000)
            except Exception as exc:
                if blocked:raise blocked[0]
                if isinstance(exc,PageIssue):raise
                kind='tls_error' if re.search(r'ERR_CERT|CERTIFICATE|SSL_',str(exc),re.I) else 'network_transient' if 'timeout' in type(exc).__name__.lower() else 'browser_error'
                raise AccessIssue('专用浏览器导航未完成，已保留入口',kind,evidence={'requested_url':url,'final_url':page.url,'redirects':redirects}) from exc
            if blocked:raise blocked[0]
            if page.url!=url:
                validate_url(page.url)
                if not same_site_redirect(url,page.url):
                    raise AccessIssue('跨站跳转已记录，暂缓读取以核验目标','unsafe_redirect',evidence={'requested_url':url,'final_url':page.url})
                if not any(r['to']==page.url for r in redirects):
                    redirects.append({'from':url,'to':page.url,'at':now()})
            self.response_issue(response,page.url,page=page)
            # The handle also remains valid after same-site canonical/mobile redirects.
            result=self.capture(self.context.pages.index(page),page.url)
            keep_open=bool(result.get('access_warning'))
            if not keep_open:self.record_success(page.url)
            result.update(requested_url=url,redirects=redirects,navigation_wait='commit_then_visible_content',
                restored_original_after_login=restore_original)
            return result
        except PageIssue as exc:
            keep_open=exc.kind in ('captcha','login_required')
            exc.evidence={'requested_url':url,'final_url':page.url,'redirects':redirects,**getattr(exc,'evidence',{})}
            self.record_issue(url,exc)
            if isinstance(exc,AccessIssue):raise
            raise AccessIssue(str(exc),exc.kind,evidence=exc.evidence) from exc
        finally:
            try:page.unroute('**/*',guard)
            except Exception:pass
            # Automated detail reads must not leave hundreds of loading tabs.
            # Explicit workspace tabs and login/CAPTCHA tabs remain available.
            self.end_operation(operation,keep=keep_open)

    def capture(self, index, expected_url=None):
        if not self.context or not 0<=index<len(self.context.pages):
            raise AccessIssue('浏览器标签已关闭，请重新打开')
        page = self.context.pages[index]
        if expected_url and page.url != expected_url:
            raise AccessIssue('标签页地址已变化，请刷新标签页列表后重试')
        validate_url(page.url)
        # Wait for business content to settle, retaining live iframe text and observed links.
        last, stable, text = '', 0, ''
        deadline = time.monotonic()+8
        while time.monotonic()<deadline:
            parts, links, frames, titles = [], [], [], []
            for frame in page.frames:
                if time.monotonic()>=deadline:break
                if frame.url!='about:blank':
                    try:
                        validate_url(frame.url)
                    except ValueError:
                        continue
                try:
                    frame_text=re.sub(r'\s+',' ',frame.locator('body').inner_text(timeout=1500)).strip()
                    frame_title=frame.title()
                    if isinstance(frame_title,str) and frame_title.strip():
                        frame_text=re.sub(r'\s+',' ',frame_title).strip()+' '+frame_text
                    parts.append(frame_text)
                    titles.extend(frame.locator('h1').all_inner_texts())
                    frames.append({'url':frame.url,'text':frame_text,'title':frame_title if isinstance(frame_title,str) else ''})
                    links.extend(frame.locator('a[href]').evaluate_all("els => els.map(a => ({url:a.href,text:(a.innerText||'').trim().slice(0,180)})).filter(a=>a.url.startsWith('https://'))"))
                except Exception:
                    continue
            text = re.sub(r'\s+',' ',' '.join(parts)).strip()
            # Gates take precedence over visible cards, including an overlay on
            # an otherwise populated page. Do not keep automating under it.
            if gate_kind(text,page.url):break
            readiness=page_readiness({'url':page.url,'text':text,'links':links})
            stable = stable+1 if text==last else 0
            if readiness['state']=='vacancy_closed' or (stable>=2 and readiness['state']!='loading_shell' and (len(text)>140 or readiness['state'] in ('job_content','empty_results'))):
                break
            last = text
            page.wait_for_timeout(500)
        gate=gate_kind(text,page.url)
        partial=gate=='login_required' and len(text)>600 and bool(re.search('岗位职责|职位描述|工作职责|任职要求',text))
        if gate:
            issue=AccessIssue('页面明确要求登录或验证，请在专用浏览器处理后重试',gate,
                evidence={'url':page.url,'title':page.title(),'visible_excerpt':text[:800]})
            self.retain(page,page.url)
            self.record_issue(page.url,issue)
            if not partial:raise issue
        readiness=page_readiness({'url':page.url,'text':text,'links':links})
        if readiness['state']=='loading_shell' or (len(text)<100 and readiness['state'] not in ('job_content','empty_results','vacancy_closed')):
            raise AccessIssue('动态页面等待后仍没有可读岗位内容；系统保留入口并继续其他页面','dynamic_unreadable',evidence={'url':page.url,'title':page.title(),'visible_excerpt':text[:600],'frame_count':len(page.frames),'readiness':readiness})
        return {'url':page.url,'title':page.title(),'text':text,'links':links,'method':'local_edge_visible_dom',
            'access_warning':'登录查看完整内容；仅整理当前公开部分' if partial else None,
            'retrieved_at':now(),'content_hash':hashlib.sha256(text.encode()).hexdigest(), 'frame_count':len(page.frames),'frames':frames,'primary_titles':titles,'readiness':readiness}

    def close(self):
        failure=None
        try:
            if self.context:
                try:
                    self.context.close()
                except Exception as exc:
                    if 'closed' not in str(exc).lower() and type(exc).__name__!='TargetClosedError':
                        failure=exc
            if self.engine:
                try:
                    self.engine.stop()
                except Exception as exc:
                    if 'closed' not in str(exc).lower():
                        failure=exc
        finally:
            self.context = self.engine = None
            self.search_page = None
            self.search_checkpoint = None
            self.verification_pages.clear()
            self.verification_urls.clear()
            self.checkpoints.clear()
            self.manual_pages.clear()
        if failure:
            raise AccessIssue('专用浏览器关闭未完成，请检查是否仍有该会话窗口') from failure
