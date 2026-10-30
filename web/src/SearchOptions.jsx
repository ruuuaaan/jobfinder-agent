import React, {useState} from 'react';
import {Globe2, Monitor, RefreshCw} from 'lucide-react';

export const SEARCH_MODES={auto:'自动选择',model:'模型联网搜索',browser:'本地浏览器搜索',tavily:'Tavily（可选）'};
export function ModePicker({value,onChange,disabled=false,nativeReady=false,tavilyReady=false}){
  const help={auto:'按本轮启用且配置完成的服务搜索；单次失败后尝试可用备用服务。',
    model:'复用模型 API 密钥。需要先测试联网能力，普通对话生成的链接不作为搜索来源。',
    browser:'在专用 Edge 中搜索必应。验证限制只影响该服务，已有页面和其他可执行工作继续处理。',
    tavily:tavilyReady?'已连接 Tavily，将使用其公开网页索引。':'仅此方式需要额外配置 Tavily 密钥。'};
  return <div className="search-mode-picker"><label className="field"><span className="field-label">搜索方式</span><select aria-label="搜索方式" value={value} disabled={disabled} onChange={e=>onChange(e.target.value)}>{Object.entries(SEARCH_MODES).map(([k,v])=><option value={k} key={k}>{v}</option>)}</select><small>{help[value]}</small></label></div>
}

export function ProviderOptions({allowed,onChange,mode}){
  return <fieldset className="provider-options"><legend>本轮允许使用的搜索服务</legend>{['model','browser','tavily'].map(p=><label className="check-label" key={p}><input type="checkbox" checked={allowed.includes(p)} disabled={mode===p} onChange={e=>onChange(e.target.checked?[...allowed,p]:allowed.filter(x=>x!==p))}/>{SEARCH_MODES[p]}</label>)}<small>仅勾选的服务会进入备用顺序；未配置或未通过测试的 API 会跳过。Tavily 需单独勾选。</small></fieldset>
}

export function NativeSettings({model,setModel,saved,busy,test}){
  return <div className="native-search-settings"><label className="field"><span className="field-label">模型联网能力（可选）</span><select aria-label="模型联网能力" value={model.native_search||'auto'} onChange={e=>setModel({...model,native_search:e.target.value})}>
    <option value="auto">按接口地址识别</option><option value="none">不使用，交给本地浏览器搜索</option><option value="deepseek">DeepSeek 搜索（复用模型密钥）</option><option value="dashscope">通义千问联网搜索（文本模型）</option><option value="responses">Responses web_search</option>
  </select><small>更改后先保存并测试模型连接。自定义兼容接口不一定支持联网；浏览器搜索仅需模型连接通过。</small></label>
  <div className="inline-actions"><button disabled={!saved?.tested||busy} onClick={test}><Globe2 size={16}/>测试模型联网</button><span className={'badge '+(saved?.native_search_tested?'MATCH':'pending')}>{saved?.native_search_tested?'联网已通过':'联网未验证，可用浏览器'}</span></div>
  <p className="micro">联网测试会发出一次真实请求，可能产生服务商费用，独立于任务预算。只接受带搜索来源的回执。</p></div>
}

export function BrowserSearchTest({run,busy,api,setBrowser}){
  const [result,setResult]=useState(null);
  const test=()=>run('browser-search-test',async()=>{
    setResult(null);
    const {command_id}=await api('/browser/search_test','POST',{});
    for(let i=0;i<90;i++){
      await new Promise(r=>setTimeout(r,700));
      const status=await api('/commands/'+command_id);
      if(status.state==='failed'){setResult({error:status.result.error});return}
      if(status.state==='done'){setResult(status.result);return}
    }
    setResult({error:'测试仍在后台执行，请稍后查看浏览器工作区。'});
  });
  return <section className="card"><div className="card-heading"><span className="section-icon"><Monitor size={20}/></span><h2>本地浏览器搜索</h2><span className="badge local">无需额外密钥</span></div>
    <p>配置并测试模型 API 后即可选用。JobFinder 在专用 Edge 窗口搜索必应，保留网页来源，再读取并整理岗位。</p>
    <div className="inline-actions"><button disabled={!!busy} onClick={test}><RefreshCw size={16}/>测试浏览器搜索</button><button onClick={()=>setBrowser(true)}>打开浏览器工作区</button></div>
    {result&&<p role="status" className={'notice '+(result.error?'warning':'')}>{result.error||`浏览器搜索完成：读取 ${result.source_count} 条来源线索。`}</p>}
    <p className="micro">测试查询为“广州 财务BP 招聘”，与任务预算分开。首次使用会打开专用 Edge；登录和验证码由你在该窗口处理。</p>
  </section>
}
