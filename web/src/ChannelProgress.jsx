import React from 'react';
import {channelState} from './channelProgress.js';

export function ChannelProgress({task, channels}) {
  return <><p className="micro">进度条表示搜索预算使用比例；用完后填满。读页和岗位整理可能仍在继续，页面问题另行保留。共享预算随其他渠道的使用同步减少。</p>
    <div className="channel-progress">{task.input.channels.map(channel=>{
      const budget=task.channel_search_progress?.[channel]||{};
      const cs=task.candidates.filter(c=>c.channel===channel);
      const needs=cs.filter(c=>c.status==='needs_help').length;
      const issues=cs.filter(c=>c.status==='system_error').length;
      const restricted=cs.filter(c=>c.status==='blocked').length;
      const status=channelState(task,channel,budget);
      const name=channels[channel]?.name||channel;
      return <div key={channel} data-channel={channel}><span>{name}</span><div>
        <div className="progress-bar" role="progressbar" aria-label={name+'搜索预算使用进度'} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(budget.percent||0)}><i style={{width:(budget.percent||0)+'%'}}/></div>
        <small>搜索 {budget.used||0} 次 · {budget.shared?'共享可用':'剩余'} {budget.remaining||0} 次 · 原文 {cs.filter(c=>c.status==='read'&&!c.evidence_packet).length} 页{needs?` · ${needs} 项需操作`:''}{issues?` · ${issues} 项系统异常`:''}{restricted?` · ${restricted} 项受限`:''}</small>
      </div><span className={'badge '+status.state}><i/>{status.label}</span></div>;
    })}</div></>;
}
