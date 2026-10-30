"""Search routing and source-only native search adapters. No generated-link parsing."""
from urllib.parse import urlsplit
from .providers import AccessIssue, post_json

MODES = {'auto':'自动选择（模型联网优先，否则浏览器）','model':'模型联网搜索','browser':'本地浏览器搜索','tavily':'Tavily（可选）'}
NATIVE_PROFILES = {'auto':'按接口地址识别','none':'不使用模型联网','deepseek':'DeepSeek 搜索（共用模型密钥）',
                   'dashscope':'通义千问联网搜索','responses':'Responses web_search'}
DASHSCOPE_HOSTS = {'dashscope.aliyuncs.com','dashscope-intl.aliyuncs.com','dashscope-us.aliyuncs.com'}

def is_dashscope(host):
    return host in DASHSCOPE_HOSTS or host.endswith('.maas.aliyuncs.com')


def native_profile(config):
    selected = config.get('native_search','auto')
    host = (urlsplit(config.get('base_url','')).hostname or '').lower()
    if selected == 'auto':
        return 'deepseek' if host == 'api.deepseek.com' else 'dashscope' if is_dashscope(host) else 'responses' if host == 'api.openai.com' else 'none'
    if selected == 'deepseek' and host != 'api.deepseek.com':
        raise AccessIssue('DeepSeek 联网搜索仅适用于官方接口地址；其他兼容服务可使用浏览器搜索')
    if selected == 'dashscope' and not is_dashscope(host):
        raise AccessIssue('通义联网搜索仅适用于百炼官方接口地址')
    if selected not in NATIVE_PROFILES:
        raise AccessIssue('模型联网方式未支持')
    return selected


def resolve_mode(mode, model):
    if mode == 'auto':
        return 'model' if model.get('native_search_tested') and native_profile(model) != 'none' else 'browser'
    if mode == 'model' and (not model.get('native_search_tested') or native_profile(model) == 'none'):
        raise AccessIssue('请先在连接设置中测试模型联网能力，或选择本地浏览器搜索')
    return mode


def task_mode(task):
    # An old task with no mode retains its Tavily route and original budget.
    return task.get('search_route') or task['input'].get('search_mode','tavily')


def clean_sources(rows):
    results, seen = [], set()
    for row in rows:
        if not isinstance(row,dict):
            continue
        url = row.get('url')
        if not isinstance(url,str):
            continue
        try:
            parts = urlsplit(url)
            if parts.scheme not in ('http','https') or not parts.hostname or parts.username or parts.password:
                continue
        except ValueError:
            continue
        if url in seen:
            continue
        seen.add(url)
        results.append({'url':url,'title':str(row.get('title') or row.get('site_name') or url)[:300],
                        'content':str(row.get('snippet') or row.get('summary') or '')[:2000]})
    return results


def token_usage(usage):
    usage = usage if isinstance(usage,dict) else {}
    def number(*keys):
        for key in keys:
            v = usage.get(key)
            if isinstance(v,(int,float)) and not isinstance(v,bool) and v >= 0:
                return int(v)
        return 0
    return {'input_tokens':number('input_tokens','prompt_tokens'), 'output_tokens':number('output_tokens','completion_tokens')}


class NativeSearch:
    def __init__(self, config):
        self.config = config
        self.profile = native_profile(config)

    @property
    def model_calls(self):
        return int(self.profile in ('dashscope','responses'))

    def search(self, query, channel=None):
        cfg, profile = self.config, self.profile
        endpoint = cfg['base_url'].rstrip('/')
        for suffix in ('/chat/completions','/responses'):
            if endpoint.endswith(suffix):
                endpoint = endpoint[:-len(suffix)]
        if profile == 'deepseek':
            if len(query)>128:
                raise AccessIssue('该服务单条检索词最长 128 字，请缩短岗位词后重试')
            response = post_json('https://api.deepseek.com/v1/tools/search', cfg['api_key'],
                                 {'query':query,'count':50,'need_content':False})
            if not isinstance(response,dict) or not isinstance(response.get('results'),list):
                raise AccessIssue('模型搜索服务未返回可验证的搜索结果列表','search_parse')
            rows = response['results']
            proof = {'kind':'provider_search_results','provider':'deepseek','reported_count':len(rows)}
        elif profile == 'dashscope':
            # The compatible Chat protocol does not expose search sources; use the
            # native protocol on the SAME configured official host with the same key.
            origin = urlsplit(endpoint)
            response = post_json(origin.scheme+'://'+origin.netloc+'/api/v1/services/aigc/text-generation/generation', cfg['api_key'], {
                'model':cfg['model'],'input':{'messages':[{'role':'user','content':'请联网查找以下招聘信息，提供原始来源：'+query}]},
                'parameters':{'enable_search':True,'search_options':{'forced_search':True,'enable_source':True,'search_strategy':'turbo'},
                              'result_format':'message','enable_thinking':False,'max_tokens':1500}})
            info = response.get('output',{}).get('search_info',{})
            if not isinstance(info,dict) or not isinstance(info.get('search_results'),list):
                raise AccessIssue('模型未返回联网来源；不采纳文本中生成的链接。可改用本地浏览器搜索','search_parse')
            rows = info['search_results']
            proof = {'kind':'provider_search_info','provider':'dashscope','reported_count':len(rows)}
        elif profile == 'responses':
            response = post_json(endpoint+'/responses', cfg['api_key'], {
                'model':cfg['model'],'input':'请联网搜索以下招聘信息，返回真实来源：'+query,
                'tools':[{'type':'web_search'}],'tool_choice':'required','max_tool_calls':1,
                'include':['web_search_call.action.sources'],'store':False,'max_output_tokens':2000},cfg.get('allow_local',False))
            calls = [o for o in response.get('output',[]) if isinstance(o,dict) and o.get('type')=='web_search_call' and o.get('status')=='completed']
            if not calls:
                raise AccessIssue('接口未实际执行联网搜索；普通文本回答不能作为搜索结果。可改用本地浏览器搜索','search_parse')
            rows = [source for call in calls for source in call.get('action',{}).get('sources',[])]
            for message in response.get('output',[]):
                if message.get('type') != 'message':
                    continue
                for part in message.get('content',[]):
                    rows.extend(a for a in part.get('annotations',[]) if a.get('type')=='url_citation')
            proof = {'kind':'completed_web_search_call','provider':'responses','tool_calls':len(calls)}
        else:
            raise AccessIssue('该模型接口尚未配置已支持的联网方式；请选择本地浏览器搜索，无需额外密钥')
        results = clean_sources(rows)
        if rows and not results:
            raise AccessIssue('搜索返回内容没有有效网页来源','search_parse')
        # A completed response without any URL cannot verify that a gateway really searched.
        if profile in ('responses','dashscope') and not results:
            raise AccessIssue('联网响应未提供来源链接；保留失败记录，不生成岗位','search_parse')
        return {'results':results,'usage':token_usage(response.get('usage',{})), 'proof':proof,
                'method':'model_native_'+profile,'model_calls':self.model_calls}
