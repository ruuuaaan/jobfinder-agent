import React,{useEffect,useState} from 'react';
import {ExternalLink,RefreshCw} from 'lucide-react';
import {browserCommand} from './SiteAccessPanel.jsx';

export const LOGIN_PLATFORMS=['zhaopin','boss','liepin','51job'];
const LABELS={unchecked:'未检查',stale:'需要重新检查',unknown:'无法确认',logged_out:'未登录',authenticated:'已确认登录',verification_required:'需要验证',restricted:'访问受限'};
const ready=s=>s?.state==='authenticated'&&(!s.access_state||s.access_state==='available');

export function LoginPreflight({task,channels,api,run,busy,notify,onDone}) {
  const selected=LOGIN_PLATFORMS.filter(c=>task.input.channels.includes(c));
  const [states,setStates]=useState({}),[publicOnly,setPublicOnly]=useState(Object.fromEntries(Object.entries(task.login_preflight?.routes||{}).map(([c,r])=>[c,r==='public']))),[message,setMessage]=useState(''),[loaded,setLoaded]=useState(false);
  useEffect(()=>{let active=true;api('/login-status').then(r=>{if(active){setStates(r.platforms);setLoaded(true)}}).catch(e=>{if(active)setMessage(e.message)});return()=>{active=false}},[api,task.id]);
  const check=async(c)=>{
    const result=await browserCommand(api,'check_login',{channel:c});
    const fresh=(await api('/login-status')).platforms;
    setStates(fresh);setLoaded(true);
    return {result,states:fresh};
  };
  const start=async(snapshot,allowPublic)=>{
    const missing=selected.filter(c=>!publicOnly[c]&&!ready(snapshot[c]));
    if(missing.length&&!allowPublic){setMessage('仍有平台未确认登录：'+missing.map(c=>channels[c].name).join('、')+'。请完成登录后检查，或选择先用可用渠道开始。');return}
    const routes=Object.fromEntries(selected.map(c=>[c,!publicOnly[c]&&ready(snapshot[c])?'session':'public']));
    await api('/tasks/'+task.id+'/login-plan','PUT',{routes});
    await api('/tasks/'+task.id+'/start','POST',{});
    await onDone();notify('任务已开始：已登录平台复用专用 Edge，其余平台按本轮选择执行');
  };
  const checkAndStart=()=>run('login-all',async()=>{
    setMessage('正在逐个检查登录；不会提交岗位查询。');let current=states;
    for(const c of selected)if(!publicOnly[c])current=(await check(c)).states;
    await start(current,false);
  });
  return <div className="login-preflight">
    <p>请在 <strong>JobFinder 打开的专用 Edge</strong> 中登录。普通 Edge 的登录不会自动同步。账号密码和验证码由你填写，不发送给模型。</p>
    <p className="micro">草稿和已有用量已保存。仅检查登录不扣搜索次数；真正搜索、失败重试和降级照常计数。{task.input.onsite_search?'已确认登录的平台优先站内搜索。':'本轮未启用站内搜索；确认的登录会话仅用于岗位详情读取。'}</p>
    <div className="login-platforms">{selected.map(c=>{
      const s=states[c]||{};
      return <div className="login-platform" key={c} data-login-channel={c}>
        <div className="site-access-heading"><strong>{channels[c].name}</strong><span className={'badge '+(ready(s)?'MATCH':'pending')}>{LABELS[s.state]||'未检查'}</span></div>
        <p>{s.reason||'检查当前专用 Edge 中的登录状态'}</p>
        {s.access_state&&s.access_state!=='available'&&<p className="danger-text">访问状态：{s.access_reason||'该站点暂不可自动访问'}；确认登录也不会解除限流或访问禁令。</p>}
        <small>上次检查：{s.checked_at?new Date(s.checked_at).toLocaleString('zh-CN'):'尚无记录'}</small>
        <div className="inline-actions"><button disabled={!!busy||!loaded} onClick={()=>run('login-open',async()=>{await browserCommand(api,'open',{url:s.url});setMessage('请在刚打开的专用 Edge 中登录；完成后点击该平台的“检查登录状态”。')})}><ExternalLink size={14}/>打开登录页面</button>
          <button disabled={!!busy} onClick={()=>run('login-check',async()=>{await check(c);setMessage('已更新登录检查。若无法确认，可展开头像菜单后再次检查。')})}><RefreshCw size={14}/>检查登录状态</button></div>
        <label className="check-label"><input type="checkbox" checked={!!publicOnly[c]} disabled={!!busy} onChange={e=>setPublicOnly({...publicOnly,[c]:e.target.checked})}/>本次仅用公开搜索</label>
      </div>;
    })}</div>
    {message&&<p className="notice" role="status">{message}</p>}
    <p className="micro">“先用可用渠道开始”会让未确认登录的平台走公开搜索，不阻塞其他渠道。登录过期或遇到验证码时，仅暂停相应网站的访问。</p>
    <div className="modal-actions"><button disabled={!!busy||!loaded} onClick={()=>run('login-start',async()=>start((await api('/login-status')).platforms,true))}>先用可用渠道开始</button><button className="primary" disabled={!!busy} onClick={checkAndStart}>检查并开始</button></div>
  </div>;
}
