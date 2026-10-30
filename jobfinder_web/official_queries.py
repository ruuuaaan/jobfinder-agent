"""Fill unused official-channel allowance with evidence-bound company searches.

No network, no extra budget, no new channel. Query generation is persisted before
dispatch; resume deduplicates against the entire saved query history.
"""
import hashlib
import re
from jobfinder.models import now
from .channel_budget import budget_policy, limits, counts


def extend_official_queries(task):
    inp=task['input']
    if task['state']!='running' or 'official_career' not in inp['channels']:
        return 0
    if budget_policy(task)!='official_priority_once':return 0
    _, pair_limits=limits(task)
    _, _, used=counts(task)
    known={q['query'] for q in task['queries']};added=0
    for city in inp['cities']:
        remaining=pair_limits[city,'official_career']-used[city,'official_career']
        if remaining<=0 or any(q['city']==city and q['channel']=='official_career' and q['status']=='pending' for q in task['queries']):continue
        proposals=[];companies=set()
        for j in task['jobs']:
            company=(j.get('company') or '').strip()
            if not 2<=len(company)<=80 or re.search(r'[\n"<>]|未披露|未知|某公司|招聘公司',company):continue
            if city not in str(j.get('discovery_cities',[]))+str(j.get('location_raw','')):continue
            if company in companies:continue
            companies.add(company)
            proposals.append((f'{company} {city} {inp["terms"][0]} 招聘 官网',
                              {'company':company,'source_url':j.get('source_url'),'kind':'observed_employer'}))
        # Bounded, distinct career-entry intents if no company evidence exists
        # yet. Never replay an identical query just to consume the allowance.
        for intent in ('企业官方网站 人才招聘','公司官网 社会招聘 职位详情','企业招聘 加入我们 工作地点','官方招聘网站 招聘职位'):
            for term in inp['terms']:
                proposals.append((f'{city} {term} {intent}',{'kind':'career_entry_intent'}))
        for text,origin in proposals:
            if not remaining:break
            if text in known or len(text)>128:continue
            known.add(text)
            task['queries'].append({'id':hashlib.sha256(text.encode()).hexdigest()[:20],
                'city':city,'channel':'official_career','query':text,'terms':list(inp['terms']),
                'kind':'index','status':'pending','plan_origin':origin,'planned_at':now(),
                'route_reason':'在已分配官网额度内补充已发现企业或独立招聘入口'})
            remaining-=1;added+=1
    if added:task['events'].append({'at':now(),'kind':'info','message':f'官网原查询已执行，按剩余额度补充 {added} 条企业/招聘入口查询；预算与累计用量不变'})
    return added
