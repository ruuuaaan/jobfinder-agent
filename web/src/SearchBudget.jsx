import React from 'react';

export const PRIORITY_BUDGET='official_priority_once';

// Display-only projection. The saved backend allocation remains authoritative.
export function budgetPreview(policy,cities,channels,budgets){
  const baseTotal=cities.reduce((n,city)=>n+(Number(budgets[city])||0),0);
  const others=channels.filter(c=>c!=='official_career');
  const priority=policy===PRIORITY_BUDGET;
  const minimum=priority?Math.max(1,others.length*2):1;
  const rows=cities.map(city=>{
    const base=Number(budgets[city])||0;
    const spare=Math.max(0,base-others.length*2);
    return {city,base,otherCount:others.length,others:others.length*2,
      official:priority&&channels.includes('official_career')?spare:0,
      unallocated:priority&&!channels.includes('official_career')?spare:0};
  });
  const invalid=priority?rows.filter(row=>row.base<minimum).map(row=>row.city):[];
  const allocatedBaseTotal=priority?rows.reduce((n,row)=>n+row.official+row.others,0):baseTotal;
  return {priority,baseTotal,minimum,rows,invalid,
    allocatedBaseTotal,unallocatedTotal:priority?rows.reduce((n,row)=>n+row.unallocated,0):0,
    maximumBonus:priority?others.length*cities.length:0,
    maximumTotal:allocatedBaseTotal+(priority?others.length*cities.length:0)};
}

export function BudgetPolicyPicker({value,onChange,legacy=false}){
  return <label className="field budget-policy"><span className="field-label">预算分配方式</span>
    <select aria-label="预算分配方式" value={value} onChange={e=>onChange(e.target.value)}>
      <option value={PRIORITY_BUDGET}>企业官网优先（其他渠道各2次）</option>
      <option value="shared">自定义共享预算</option>
    </select>
    {legacy&&<small>旧草稿沿用原共享规则；可主动切换为企业官网优先。</small>}
  </label>;
}

export function BudgetPreview({value,compact=false}){
  if(!value.priority)return <p className="micro">各渠道共享城市预算；可单独设置渠道上限。失败不额外补充额度。</p>;
  return <div className={'budget-policy-preview '+(compact?'compact':'')}>
    <p>每城其他已选渠道各2次，剩余额度给企业官网。</p>
    {value.rows.map(row=><div className="budget-preview-city" key={row.city}>
      <strong>{row.city}</strong>
      <span>企业官网 {row.official} 次 · 其他 {row.otherCount} 个渠道各2次</span>
      {row.unallocated>0&&<small>未启用企业官网，{row.unallocated} 次保留未分配。</small>}
    </div>)}
    <p>每城每个其他渠道首次搜索失败后额外补1次，最多补一次；不挤占官网额度。成功但无岗位不补。</p>
    <p className="budget-maximum">基础 {value.baseTotal} 次{value.unallocatedTotal>0?`（已分配 ${value.allocatedBaseTotal} 次）`:''} · 最多补充 {value.maximumBonus} 次 · 含补充最多 {value.maximumTotal} 次</p>
    {value.invalid.length>0&&<p role="alert" className="notice warning">{value.invalid.join('、')}的基础预算不足：所选其他渠道每城至少需要 {value.minimum} 次。请增加预算或减少渠道；不会自动提高预算。</p>}
  </div>;
}

export function SavedBudgetSummary({summary,channels}){
  if(!summary)return null;
  return <div className="budget-grants">
    <p><strong>{summary.policy===PRIORITY_BUDGET?'企业官网优先':'自定义共享预算'}</strong></p>
    <p>基础预算 {summary.base_total} 次 · 已补充 {summary.bonus_total} 次 · 当前可执行上限 {summary.effective_total} 次</p>
    {summary.unallocated_total>0&&<p>尚有 {summary.unallocated_total} 次基础预算未分配；未自动启用企业官网。</p>}
    {summary.policy===PRIORITY_BUDGET&&<><p>最多可执行 {summary.maximum_total} 次。每城每个其他渠道仅首次搜索失败补1次，补过后不再补；零结果不触发。</p>
      <details><summary>查看各城市、渠道的额度</summary>{Object.entries(summary.per_city||{}).map(([city,data])=><div key={city}>
        <p><strong>{city}</strong> · 已用 {data.used} / {data.limit} 次</p>
        <table><thead><tr><th>渠道</th><th>基础</th><th>补充</th><th>已用 / 上限</th></tr></thead><tbody>{Object.entries(data.channels||{}).map(([channel,row])=><tr key={channel}><td>{channels[channel]?.name||channel}</td><td>{row.base}</td><td>{row.bonus}{row.granted?'（已补）':''}</td><td>{row.used} / {row.limit}</td></tr>)}</tbody></table>
      </div>)}</details>
    </>}
  </div>;
}
