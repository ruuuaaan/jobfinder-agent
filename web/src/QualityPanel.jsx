import React from 'react';

export function QualityPanel({task,channels}){
  if(task.historical)return null;
  const e=task.evidence_counts||{};
  const p=task.execution_progress||{};
  return <section className="card"><div className="card-heading"><h2>证据与覆盖范围</h2></div>
    <p>表内岗位：搜索摘要 {e.search_snippet||0} 条 · 站内列表 {e.onsite_list||0} 条 · 原页记录 {e.original_detail||0} 条</p>
    <p className="micro">职责和要求完整 {task.complete_count||0} 条。在招状态单独核验；摘要、列表及缺失信息均保留待确认。</p>
    <p className="micro">历史入口：已尝试 {p.known_attempted||0}/{p.known_entries||0} 个，整理完成 {p.known_read||0} 个，待处理 {p.known_pending||0} 个。未尝试不代表没有岗位。</p>
    {task.execution_policy_version>=2&&<p className="micro">摘要整理已用 {p.snippet_calls||0}/{p.snippet_call_limit||0} 次模型调用；{p.snippet_reserve_released?'页面额度已用完，原文已整理，剩余额度用于已有摘要。':'其余优先用于岗位原文。'}{p.withheld_records||0} 条相关性不足的记录保留在线索中，未放入岗位表。</p>}
    <details><summary>哪些岗位词实际搜过？</summary><div className="coverage-list">{(task.query_coverage||[]).map(r=><div key={r.city+r.channel}>
      <strong>{r.city} · {channels[r.channel]?.name||r.channel}</strong><p>已尝试：{r.attempted_terms.join('、')||'无'}（完成 {r.completed}/{r.attempts} 次）</p>
      <small>未执行：{r.not_run_terms.join('、')||'无'}</small></div>)}</div></details>
    {task.input.onsite_search&&<p className="micro">{task.execution_policy_version>=2?'智联先试站内；BOSS、猎聘、前程无忧先查公开索引，覆盖渠道后用剩余额度补充站内搜索。站内失败会安排索引回退，每次尝试均计入累计用量。':'此历史任务沿用原有站内优先计划；实际执行方式见查询记录。'}</p>}
  </section>
}
