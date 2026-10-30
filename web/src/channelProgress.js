// A full bar means search allowance is spent, not that every job detail was read.
export function channelState(task, channel, budget) {
  if (budget?.unallocated) return {state:'pending', label:'未分配搜索预算'};
  if (budget?.exhausted) return {state:'budget_exhausted', label:'搜索预算已用完'};
  if (task.state==='cancelled') return {state:'cancelled', label:'已停止'};
  if (task.state==='paused') return {state:'paused', label:'已暂停'};
  if (task.state==='limit_reached') return {state:'limit_reached', label:'已到用量限制'};
  const cs=task.candidates.filter(c=>c.channel===channel);
  if (cs.some(c=>c.status==='needs_help')) return {state:'needs_help', label:'待登录／验证'};
  if (cs.some(c=>c.status==='system_error')) return {state:'partial', label:'有系统异常'};
  if (task.state==='running') return {state:'running', label:budget?.used?'正在收集':'等待执行'};
  if (cs.some(c=>c.status==='blocked')) return {state:'blocked', label:'有访问限制'};
  if (budget?.used) return {state:'partial', label:'已执行部分搜索'};
  return {state:'pending', label:'尚未执行'};
}
