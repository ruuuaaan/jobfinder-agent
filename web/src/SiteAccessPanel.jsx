import React, {useEffect, useRef, useState} from 'react';
import {ExternalLink, Globe2, LoaderCircle, RefreshCw} from 'lucide-react';

const SITES = [
  {site:'zhipin.com', name:'BOSS直聘', url:'https://www.zhipin.com/'},
  {site:'zhaopin.com', name:'智联招聘', url:'https://www.zhaopin.com/'},
  {site:'liepin.com', name:'猎聘', url:'https://www.liepin.com/'},
  {site:'51job.com', name:'前程无忧（51job）', url:'https://www.51job.com/'},
];
const LABELS = {needs_help:'待登录／验证', cooldown:'等待恢复', blocked:'访问受限', available:'当前未暂停'};
const normalizeSite = value => {
  const aliases = {boss:'zhipin.com', zhipin:'zhipin.com', zhaopin:'zhaopin.com', liepin:'liepin.com', '51job':'51job.com'};
  const site = String(value||'').toLowerCase().replace(/^https?:\/\//,'').split('/')[0];
  return aliases[site] || SITES.find(s=>site===s.site||site.endsWith('.'+s.site))?.site || site;
};
const validUrl = url => typeof url==='string' && /^https?:\/\//.test(url);

export async function browserCommand(api, action, body={}) {
  const {command_id} = await api('/browser/'+action,'POST',body);
  for (let i=0; i<100; i++) {
    await new Promise(resolve=>setTimeout(resolve,700));
    const result = await api('/commands/'+command_id);
    if (result.state==='failed') throw new Error(result.result?.error || '浏览器操作未完成，请稍后重试');
    if (result.state==='done') return {id:command_id,...result.result};
  }
  throw new Error('操作仍在后台处理中，请稍后刷新网站状态');
}

export function SiteAccessPanel({api,run,busy,notify,taskId,onResumed,onOpened,quickLinks=false}) {
  const [snapshot,setSnapshot] = useState(null);
  const [error,setError] = useState('');
  const [loading,setLoading] = useState(false);
  const [clock,setClock] = useState(Date.now());
  const mounted = useRef(false);
  const refreshing = useRef(null);
  const selectedTask = useRef(taskId);
  selectedTask.current = taskId;
  const reload = async () => {
    if (taskId!==selectedTask.current || (refreshing.current && refreshing.current.taskId===taskId)) return null;
    const request = {taskId};
    refreshing.current = request;
    const isCurrent = () => mounted.current && refreshing.current===request && selectedTask.current===taskId;
    if (mounted.current) setLoading(true);
    try {
      const data = await api('/site-access'+(taskId?'?task_id='+encodeURIComponent(taskId):''));
      if (isCurrent()) {setSnapshot(data);setError('');}
      return data;
    } catch (e) {
      if (isCurrent()) setError(e.message);
      return null;
    } finally {
      if (isCurrent()) setLoading(false);
      if (refreshing.current===request) refreshing.current = null;
    }
  };
  useEffect(()=>{
    mounted.current = true;
    setSnapshot(null);
    setError('');
    reload();
    const refreshTimer = setInterval(reload,5000);
    const clockTimer = setInterval(()=>setClock(Date.now()),1000);
    return ()=>{mounted.current=false;clearInterval(refreshTimer);clearInterval(clockTimer);};
  },[api,taskId]);
  const records = new Map();
  for (const item of snapshot?.sites || []) records.set(normalizeSite(item.site||item.url),item);
  const sites = [...records].map(([site,item])=>({name:site,...SITES.find(s=>s.site===site),...item,site}));
  const open = site => run('site-open',async()=>{
    const result = await browserCommand(api,'open',{url:site.url});
    onOpened?.(result);
    notify('已打开专用 Edge，请在该窗口手动完成登录或验证');
  });
  const resume = site => run('site-resume',async()=>{
    let result;
    try {
      result = await browserCommand(api,'resume_site',{url:site.url,...(taskId?{task_id:taskId}:{})});
    } finally {
      await reload();
      await onResumed?.();
    }
    if (result.ready===false) {
      notify(result.reason || result.message || '仍需登录或验证，请在专用 Edge 中完成后再次检查',true);
      return;
    }
    notify(result.message || (result.ready?'网站检查已通过；请查看更新后的状态和任务进度':'网站检查已完成，请查看更新后的状态；仍需验证时请在专用 Edge 中继续处理'));
  });
  return <section className="card site-access-panel" aria-label="网站访问状态">
    <div className="card-heading"><Globe2 size={20}/><h2>网站访问状态</h2><button className="text-button" disabled={loading} onClick={reload} aria-label="刷新网站状态">{loading?<LoaderCircle size={15} className="spin"/>:<RefreshCw size={15}/>}刷新</button></div>
    <p className="micro">{taskId?'仅显示当前任务相关的站点，包括页面跳转后的网站；其他任务的站点不在此展示。':'此处仅显示四个预登录网站；具体任务的站点请在任务详情查看。'}</p>
    <p className="micro">BOSS直聘、智联招聘、猎聘、前程无忧（51job）的详情统一使用专用 Edge。请先在该窗口手动登录；不会自动填写或提交账号密码。</p>
    {quickLinks&&<div className="site-shortcuts" aria-label="四站预登录入口">{SITES.map(site=>{
      const state=records.get(site.site)?.state;
      return <button key={site.site} disabled={!!busy||!!error||state==='cooldown'||state==='blocked'} onClick={()=>open(site)}><ExternalLink size={14}/>打开{site.name}预登录</button>;
    })}</div>}
    {error&&<p className="notice warning" role="alert">网站状态更新失败：{error}。当前显示可能已过时，请刷新后再操作。</p>}
    {snapshot&&!sites.length&&!error&&<p className="muted">当前任务暂无网站访问记录。</p>}
    <div className="site-access-list">{sites.map(site=>{
      const remaining=Math.max(0,Math.ceil((Number(site.retry_at)||0)-clock/1000));
      const canResume=site.state==='needs_help'&&['login_required','captcha'].includes(site.kind);
      return <div className="site-access-item" key={site.site} data-site={site.site}>
        <div className="site-access-heading"><strong>{site.name}</strong><span className={'badge '+(site.state||'pending')}>{LABELS[site.state]||'待检查'}</span></div>
        {site.reason&&<p>{site.reason}</p>}
        {site.state==='cooldown'&&<p className="site-cooldown">{remaining>0?`至少等待 ${remaining} 秒后再检查`:'等待后台检查恢复'}；此时不能手动提前恢复。</p>}
        {canResume&&<div className="site-access-actions"><button disabled={!!busy||!!error||!validUrl(site.url)} onClick={()=>open(site)}><ExternalLink size={14}/>打开验证页面</button><button className="primary" disabled={!!busy||!!error||!validUrl(site.url)} onClick={()=>resume(site)}>{busy==='site-resume'&&<LoaderCircle size={14} className="spin"/>}验证已完成，检查并继续</button></div>}
      </div>;
    })}</div>
    <p className="micro">“当前未暂停”不代表已登录。刷新仅更新网站状态，不改变任务状态或预算。{taskId?'检查通过后，仅从保存位置继续本任务在该网站受阻的项目，已用预算保留。':'检查通过后解除该网站暂停；需继续任务时，请在任务详情中操作。'}</p>
    {snapshot?.policy&&<p className="micro">同站访问至少间隔 {snapshot.policy.min_interval_seconds} 秒；网站未提供等待时间时，限流默认等待 {snapshot.policy.rate_limit_cooldown_seconds} 秒。</p>}
  </section>;
}
