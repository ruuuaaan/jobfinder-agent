"""Public search controls in JobFinder's own Edge; no private recruitment APIs."""
import hashlib
import re
import time
from urllib.parse import urlsplit, unquote
from jobfinder.models import now
from .providers import AccessIssue, validate_url
from .recovery import gate_kind, PageIssue
from .browser import same_site_redirect

ROOTS={'zhaopin':'https://www.zhaopin.com/','liepin':'https://www.liepin.com/',
       '51job':'https://www.51job.com/','boss':'https://www.zhipin.com/'}
# This path was observed in the public city navigation. No inferred numeric city codes.
CITY_ENTRIES={('zhaopin','广州'):'https://www.zhaopin.com/guangzhou/'}
DETAIL_RE=r'/job_detail/|/job/\d+\.shtml|/jobdetail/CC|/CC[L\d]|/\d+\.html'


def observe(page):
    return page.evaluate('''() => {
      const visible=e=>!!e.getClientRects().length;
      const cityFilters=[];
      for(const label of document.querySelectorAll('label,span,div,button')){
        if(!visible(label)||! /^(地区|工作地点|城市|选择城市|切换城市)$/.test(label.innerText.trim()))continue;
        for(let p=label.parentElement,i=0;p&&i<3;p=p.parentElement,i++){
          const text=(p.innerText||'').trim();
          if(text.length>220||p.querySelector('a[href*="jobdetail"],a[href*="job_detail"]'))break;
          cityFilters.push({label:label.innerText.trim(),text});
        }
      }
      return {url:location.href,title:document.title,text:document.body?.innerText||'',city_filters:cityFilters,
      inputs:[...document.querySelectorAll('input')].map((e,i)=>({index:i,placeholder:e.placeholder,type:e.type,value:e.value,visible:!!e.getClientRects().length})),
      result_headings:[...document.querySelectorAll('h1,h2,h3,[role="status"],.search-result-title,.result-title')].filter(visible).map(e=>(e.innerText||'').trim()),
      links:[...document.querySelectorAll('a[href]')].filter(visible).map(a=>({text:(a.innerText||'').trim(),url:a.href})).filter(a=>/^https?:/.test(a.url))};
    }''')


def city_filter_evidence(observation, city, channel):
    """Require the selected control or an explicit result heading, never card text.

    A click, a city landing URL, or the desired city appearing somewhere in the
    page is not evidence that the submitted search retained its city filter.
    """
    target=re.escape(city.removesuffix('市'))+r'市?'
    evidence=[]
    for item in observation.get('city_filters',[]):
        label=item.get('label','');text=item.get('text','')
        if re.fullmatch(r'\s*'+re.escape(label)+r'\s*[:：]?\s*'+target+r'\s*[▽▼∨⌄×]?\s*',text):
            evidence.append({'kind':'selected_city_control','text':text})
    # Zhaopin's result heading names the actual city (including when a stale
    # profile changes a Tianjin request to Guangzhou). Do not read job-card
    # titles or infer jl numeric codes from the requested city.
    if channel=='zhaopin' and urlsplit(observation.get('url','')).path.rstrip('/')=='/jobs':
        title=observation.get('title','')
        if re.match(r'^\s*'+target+r'(?:热门职位招聘|职位招聘|人才招聘|招聘)',title):
            evidence.append({'kind':'result_city_heading','text':title})
    return evidence


def click_unique_visible(page, text, before_click=None):
    options=[x for x in page.get_by_text(text,exact=True).all() if x.is_visible()]
    if len(options)!=1:return False
    if before_click:before_click()
    options[0].click(timeout=5000)
    return True


def current_result_city(observation):
    """Read the actual city from the result heading, never numeric query codes."""
    match=re.match(r'^\s*([^\s]{2,12}?)(?:热门职位招聘|职位招聘|人才招聘)',observation.get('title',''))
    return match[1] if match else None


def collect_cards(page):
    # Inspect rendered DOM structure each run. Ancestors must contain a single
    # observed vacancy link, and exclude navigation/full-page text containers.
    return page.evaluate(r'''() => {
      const rx=/\/job_detail\/|\/job\/\d+\.shtml|\/jobdetail\/CC|\/CC[L\d]|\/\d+\.html/i;
      const detail=a=>/^https?:/.test(a.href)&&rx.test(new URL(a.href).pathname);
      const anchors=[...document.querySelectorAll('a[href]')].filter(a=>a.getClientRects().length&&detail(a)&&a.innerText.trim());
      const out=[],seen=new Set();
      for(const a of anchors){
        if(seen.has(a.href))continue;seen.add(a.href);
        let el=a;
        for(let p=a.parentElement,i=0;p&&i<6;p=p.parentElement,i++){
          const ids=new Set([...p.querySelectorAll('a[href]')].filter(detail).map(x=>x.href));
          if(ids.size>1||p.innerText.length>2200)break;
          el=p;
        }
        if(!/广告|招聘公告|招聘会/.test(a.innerText))out.push({url:a.href,title:(a.innerText||'').trim(),content:el.innerText.trim()});
      }
      return out;
    }''')


def submission_evidence(observation, before, term):
    """Accept visible result transitions, including SPA updates at the same URL."""
    current=observation.get('url','');previous=before.get('url','')
    path=urlsplit(current).path
    query=unquote(urlsplit(current).query)
    input_matches=any(i.get('visible') and i.get('value')==term for i in observation.get('inputs',[]))
    headings=observation.get('result_headings',[])
    matches=[h for h in headings if term in h and re.search(r'搜索|结果|相关职位|招聘',h)
        and h not in before.get('result_headings',[])]
    if current!=previous and re.search(r'/(?:jobs?|search|zhaopin)(?:[/_.-]|$)',path,re.I) and (term in query or input_matches or matches):
        return [{'kind':'observed_result_navigation','url':current}]
    changed=(observation.get('text','')!=before.get('text','')
        or observation.get('links',[])!=before.get('links',[]))
    if not changed:return []
    if matches:return [{'kind':'visible_query_result_heading','text':h} for h in matches]
    explicit_result=bool(re.search(r'搜索结果|共\s*\d+\s*(?:个|条)?\s*(?:职位|岗位)|暂无相关职位|未找到相关',observation.get('text','')))
    if input_matches and explicit_result:
        return [{'kind':'visible_query_and_updated_results','term':term}]
    if current!=previous and term in query and explicit_result:
        return [{'kind':'observed_query_url_and_results','url':current}]
    return []


def search(browser,channel,city,term):
    entry=CITY_ENTRIES.get((channel,city),ROOTS[channel])
    # A persisted site gate must be checked before a new tab, DNS, or action.
    if browser.access:
        try:browser.access.check(entry)
        except PageIssue as exc:raise AccessIssue(str(exc),exc.kind,evidence=exc.evidence) from exc
    operation=browser.begin_operation(('onsite',channel,city,term),entry)
    page=operation['page'];checkpoint=operation['checkpoint'] or {}
    events=list(checkpoint.get('events',[]));submitted=checkpoint.get('submitted',False)
    baseline=checkpoint.get('baseline',{});city_evidence=[];query_evidence=[]
    keep=False;blocked=[];observed_responses=[]

    def proof(obs):
        return {'kind':'onsite_visible_search','url':obs.get('url',page.url),'title':obs.get('title',''),
            'retrieved_at':now(),'text':obs.get('text',''),'events':events,
            'submission_evidence':query_evidence,
            'city_filter':{'requested_city':city,'verified':bool(city_evidence),'evidence':city_evidence},
            'content_hash':hashlib.sha256(obs.get('text','').encode()).hexdigest()}

    def navigation_guard(route):
        request=route.request
        if request.is_navigation_request():
            try:
                frame=request.frame;target_page=frame.page
                owned=target_page in operation['pages'] or target_page.opener() in operation['pages']
                main=frame==target_page.main_frame
                if owned:operation['pages'].add(target_page)
            except Exception:
                # Popup requests can precede creation of their frame. Keep the
                # short-lived operation guard conservative until one exists.
                owned=main=True
            if owned and main:
                try:
                    if not same_site_redirect(entry,request.url):
                        raise AccessIssue('站内页面跨站跳转已停止，请核验目标','unsafe_redirect',evidence={'requested_url':entry,'final_url':request.url})
                    browser.before_request(request.url)
                    validate_url(request.url)
                except (ValueError,PageIssue) as exc:
                    blocked.append(exc);route.abort();return
        route.fallback()

    def response_seen(response):
        try:
            frame=response.frame;target_page=frame.page
            owned=target_page in operation['pages'] or target_page.opener() in operation['pages']
            if owned and response.request.is_navigation_request() and frame==target_page.main_frame:
                observed_responses.append((response,target_page))
        except Exception:pass

    def check_responses():
        # The response event can precede the document body. Classify at the
        # next DOM observation so a visible 403 login/challenge can be retained.
        while observed_responses:
            response,target_page=observed_responses.pop(0)
            browser.response_issue(response,response.url,page=target_page)

    def checked_observe():
        if blocked:raise blocked[0]
        check_responses()
        if page.url!='about:blank' and not same_site_redirect(entry,page.url):
            raise AccessIssue('站内页面已跨站跳转，请核验目标','unsafe_redirect',evidence={'requested_url':entry,'final_url':page.url})
        obs=observe(page)
        browser.check_gate(page,obs)
        return obs

    def checked_goto(url):
        browser.before_request(url);validate_url(url)
        try:
            response=page.goto(url,wait_until='domcontentloaded',timeout=25000)
            if observed_responses:check_responses()
            else:browser.response_issue(response,page.url,page=page)
        except PageIssue:raise
        except Exception as exc:
            if blocked:raise blocked[0]
            if not page.url.startswith('https://'):raise AccessIssue('站内入口导航失败','browser_error') from exc
            events.append({'action':'navigation_timeout','url':page.url})
        return checked_observe()

    def before_control():
        checked_observe()
        browser.before_request(page.url)

    def click(text):return click_unique_visible(page,text,before_control)

    try:
        browser.context.route('**/*',navigation_guard)
        browser.context.on('response',response_seen)
        if checkpoint:
            events.append({'action':'resume_retained_page','url':page.url,'submitted':submitted})
            obs=checked_observe()
            controls=any(i.get('visible') and i.get('type') not in ('password','tel','hidden')
                and re.search('职位|公司|关键词|搜索',i.get('placeholder','')) for i in obs.get('inputs',[]))
            returned_home=urlsplit(page.url).path in ('','/') or page.url!=checkpoint.get('resume_url',page.url)
            if submitted and controls and returned_home and not submission_evidence(obs,baseline,term):
                submitted=False
                events.append({'action':'restore_search_after_login','url':page.url})
        else:
            obs=checked_goto(entry)
        if not submitted:
            if channel=='zhaopin' and '求职安全提示' in obs.get('text','') and click('我知道了'):
                events.append({'action':'acknowledge_job_safety_notice'})
                obs=checked_observe()
            city_links={a['url'] for a in obs['links'] if a['text'] in (city,city.removesuffix('市')+'市')
                and same_site_redirect(entry,a['url']) and not re.search(DETAIL_RE,urlsplit(a['url']).path,re.I)}
            if channel=='zhaopin' and not city_links:
                # The root page can use the account's previous city. Follow the
                # observed city directory, then its real target-city link.
                maps={a['url'] for a in obs['links'] if a['text'].strip() in ('城市频道','切换','[切换]','切换城市')
                      and same_site_redirect(entry,a['url']) and urlsplit(a['url']).path.rstrip('/')=='/citymap'}
                if len(maps)==1:
                    directory=next(iter(maps));obs=checked_goto(directory)
                    events.append({'action':'observed_city_directory','url':directory})
                    city_links={a['url'] for a in obs['links'] if a['text'].strip() in (city,city.removesuffix('市')+'市')
                                and same_site_redirect(entry,a['url']) and not re.search(DETAIL_RE,urlsplit(a['url']).path,re.I)}
            if len(city_links)==1:
                city_entry=next(iter(city_links))
                if city_entry!=page.url:
                    events.append({'action':'observed_city_entry','city':city,'url':city_entry})
                    obs=checked_goto(city_entry)
            deadline=time.monotonic()+8
            while True:
                obs=checked_observe()
                inputs=[i for i in obs['inputs'] if i['visible'] and re.search('职位|公司|关键词|搜索',i['placeholder']) and i['type'] not in ('password','tel','hidden')]
                if inputs or time.monotonic()>deadline:break
                page.wait_for_timeout(400)
            if not inputs:
                link=next((a for a in obs['links'] if a['text'] in ('职位','搜索','找工作') and same_site_redirect(entry,a['url'])),None)
                if link:
                    events.append({'action':'observed_search_entry','url':link['url']})
                    checked_goto(link['url']);page.wait_for_timeout(1000);obs=checked_observe()
                    inputs=[i for i in obs['inputs'] if i['visible'] and re.search('职位|公司|关键词|搜索',i['placeholder']) and i['type'] not in ('password','tel','hidden')]
            if not inputs:raise AccessIssue('站内页未找到可操作的岗位搜索框','onsite_controls_missing')
            baseline=obs
            before_control()
            # Re-observe after the pacing wait; hydration may replace inputs.
            obs=checked_observe()
            inputs=[i for i in obs['inputs'] if i['visible'] and re.search('职位|公司|关键词|搜索',i['placeholder']) and i['type'] not in ('password','tel','hidden')]
            if not inputs:raise AccessIssue('等待后搜索框已变化，保留原页','onsite_controls_missing')
            field=page.locator('input').nth(inputs[0]['index'])
            field.fill(term)
            # City landing pages can ignore Enter. Submit the observed unique
            # search button once; use Enter only when no such control exists.
            if click('搜索'):
                events.append({'action':'click_visible_search_button'})
            else:
                before_control();field.press('Enter')
            submitted=True
            events.append({'action':'submit','term':term,'city_filter':'pending_result_city_verification'})
        deadline=time.monotonic()+25;previous='';stable=0;seen_pages={page}
        opened_city_filter=False;city_clicked=False;confirm_clicked=False;tried_controls=set()
        while time.monotonic()<deadline:
            # Only an operation's own popup pages may be adopted or cleaned up.
            fresh=[p for p in browser.context.pages if p in operation['pages'] and p not in seen_pages and not p.is_closed()]
            navigated=[p for p in fresh if p.url.startswith('https://') and same_site_redirect(entry,p.url)]
            if navigated:
                page=navigated[-1];operation['page']=page
                # Do not oscillate back to an earlier opener in later polls.
                seen_pages.update(navigated)
            fresh=[p for p in fresh if p not in seen_pages]
            if any(p.url=='about:blank' for p in fresh):
                page.wait_for_timeout(300);continue
            try:
                obs=checked_observe()
                query_evidence=submission_evidence(obs,baseline,term)
                city_evidence=city_filter_evidence(obs,city,channel)
                if query_evidence and not city_evidence:
                    if opened_city_filter and not city_clicked:
                        city_clicked=click(city) or click(city.removesuffix('市')+'市')
                        if city_clicked:
                            events.append({'action':'observed_city_option_clicked','city':city})
                            previous='';stable=0;page.wait_for_timeout(500);continue
                    if city_clicked and not confirm_clicked:
                        confirm_clicked=click('确定') or click('确认')
                        if confirm_clicked:
                            events.append({'action':'observed_city_selection_confirmed','city':city})
                            previous='';stable=0;page.wait_for_timeout(500);continue
                    if not city_clicked:
                        # Current pages show the chosen city itself as the
                        # clickable trigger; 地区 may only open district options.
                        labels=[current_result_city(obs),'选择城市','切换城市','城市','地区','工作地点']
                        for label in (x for x in labels if x):
                            if label in tried_controls:continue
                            tried_controls.add(label)
                            if click(label):
                                opened_city_filter=True
                                events.append({'action':'observed_city_filter_opened','label':label})
                                page.wait_for_timeout(500);break
                        else:opened_city_filter=False
                        if opened_city_filter:continue
                cards=collect_cards(page)
            except PageIssue:raise
            except Exception as exc:
                if 'context was destroyed' in str(exc).lower() or 'navigation' in str(exc).lower():
                    page.wait_for_timeout(300);continue
                raise
            stable=stable+1 if previous==obs['text'] else 0;previous=obs['text']
            if query_evidence and stable>=2 and (cards or re.search('暂无相关职位|没有找到|没有搜索到|未找到相关',obs['text'])):break
            page.wait_for_timeout(500)
        if page.url=='about:blank':raise AccessIssue('搜索新标签仍未完成跳转，待页面恢复后重试','onsite_navigation_pending')
        obs=checked_observe();query_evidence=submission_evidence(obs,baseline,term)
        city_evidence=city_filter_evidence(obs,city,channel);cards=collect_cards(page)
        if not query_evidence:raise AccessIssue('输入后尚未确认进入搜索结果，未把首页推荐或广告计为岗位','onsite_submission_unconfirmed')
        if channel=='zhaopin' and not city_evidence:
            raise AccessIssue(f'站内城市筛选未确认生效（目标：{city}）；当前结果未纳入岗位，改用允许的公开索引查询','onsite_city_unconfirmed')
        cards=[c for c in cards if same_site_redirect(entry,c['url'])]
        if not cards and not re.search('暂无相关职位|没有找到|没有搜索到|未找到相关',obs['text']):
            raise AccessIssue('搜索已提交，未读到可绑定独立链接的岗位卡片','onsite_cards_unreadable')
        browser.record_success(page.url)
        return {'results':cards,'usage':{},'method':'local_edge_onsite','proof':proof(obs),
            'coverage':{'city':city,'term':term,'submitted':True,'cards':len(cards),'scope':'首批公开列表',
                'city_filter':'verified_visible_city' if city_evidence else '未确认原站城市筛选，按岗位地点核验',
                'city_verified':bool(city_evidence),'city_evidence':city_evidence,
                'education_filter':'不使用原站学历单选，按本轮有效条件核验岗位原文','more_pages':'not_run',
                'detail_attempts':'逐卡链接加入任务队列，受网页与模型预算约束'}}
    except Exception as exc:
        try:obs=observe(page)
        except Exception:obs={'url':page.url}
        issue=exc if isinstance(exc,AccessIssue) else AccessIssue(str(exc),exc.kind,evidence=exc.evidence) if isinstance(exc,PageIssue) else AccessIssue('站内搜索操作未完成，保留原页与原因','onsite_browser_error')
        if getattr(exc,'_site_recorded',False):issue._site_recorded=True
        keep=issue.kind in ('captcha','login_required')
        browser.record_issue(entry,issue)
        issue.receipt=proof(obs)|{'submitted':submitted,'error_type':type(exc).__name__,'error_detail':str(exc)[:400]}
        raise issue
    finally:
        try:browser.context.unroute('**/*',navigation_guard)
        except Exception:pass
        try:browser.context.remove_listener('response',response_seen)
        except Exception:pass
        operation['page']=page
        browser.end_operation(operation,keep=keep,checkpoint={'requested_url':entry,'resume_url':page.url,'events':events,'submitted':submitted,'baseline':baseline})
