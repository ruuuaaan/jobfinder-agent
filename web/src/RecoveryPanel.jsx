import React from 'react';
import {Monitor, AlertCircle} from 'lucide-react';

export const OUTCOMES={needs_help:'需要登录／验证',system_error:'系统异常',blocked:'访问限制',filtered:'暂缓线索',retry_pending:'等待有限重试',pending_extract:'分批整理中',pending:'尚未读取',reading:'正在读取',read:'整理完成'};
export function candidateLabel(c){
  const batches=(c.batches||[]).filter(b=>b.status!=='split');
  return (OUTCOMES[c.status]||c.status)+(batches.length?` · 批次 ${batches.filter(b=>b.status==='done').length}/${batches.length}`:'');
}
export function RecoveryPanel({task,busy,act,setBrowser,failedQueries}){
  const groups=[['needs_help','需要你操作','仅明确要求登录或验证码的页面列在这里。'],['system_error','系统异常','模型、连接或附件解析问题，不需要逐个登录。'],['blocked','访问限制','保留原因与链接，程序跳过受限入口。'],['retry_pending','自动重试中','仅短暂连接故障，最多自动再试一次，读取次数照常累计。'],['filtered','暂缓线索','保留发现记录；优先处理与本次需求相关的岗位入口。']];
  return <section className="card recovery-card"><div className="card-heading"><AlertCircle size={20}/><h2>页面处理情况</h2></div>
    <p className="micro">{task.state==='running'?'正在处理其余可读页面。':'当前任务未运行，点击继续后处理剩余页面。'} 已完成整理批次 {task.batch_progress?.done||0} / {task.batch_progress?.total||0}。</p>
    {groups.map(([status,title,hint])=>{const items=task.candidates.filter(c=>c.status===status);return items.length>0&&<details key={status} open={status==='needs_help'}><summary>{title}<span className={'badge '+status}>{items.length}</span></summary><p className="micro">{hint}</p><div className="recovery-items">{items.map(c=><div className="help-item" key={c.id}><a href={c.url} target="_blank" rel="noreferrer">{c.title}</a><small>{c.issue?.message||c.error||c.selection?.reason}</small>{c.issue?.evidence?.final_url&&<small>跳转目标：{c.issue.evidence.final_url}</small>}</div>)}</div>
      {status==='needs_help'&&<><p className="micro">在专用 Edge 中完成验证后，使用“网站访问状态”中的“验证已完成，检查并继续”。尚未显示的网站，可先在浏览器工作区打开该页面。</p><div className="inline-actions"><button onClick={()=>setBrowser(true)}><Monitor size={15}/>打开浏览器工作区</button></div></>}
      {status==='system_error'&&<button disabled={!!busy||task.state==='running'} onClick={()=>act('retry-system')}>重试系统异常（继续计量）</button>}
    </details>})}
    {failedQueries.length>0&&<p className="notice warning">搜索请求异常：{failedQueries.at(-1).error}。搜索次数仍累计，请查看搜索记录。</p>}
    {!task.candidates.some(c=>groups.some(([s])=>s===c.status))&&<p className="micro">目前没有等待处理的异常。</p>}
  </section>
}
