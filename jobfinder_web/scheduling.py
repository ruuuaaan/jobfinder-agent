"""Budget-aware source verification. No requests, no hidden budget increases."""
from .recovery import ready


def evidence_priority(task,c):
    """Hints order reads, never decide a job's city or education as facts."""
    text=(c.get('title','')+' '+c.get('summary','')).casefold()
    city=any(city in text for city in task['input']['cities'])
    education=task['input'].get('education')
    words={'at_least_bachelor':('本科','硕士','博士'),'bachelor':('本科',),'at_least_master':('硕士','博士')}.get(education,())
    degree=not words or any(w in text for w in words)
    return 0 if city and degree else 1 if city else 2


def batch_calls(c):
    return c.get('model_attempts',sum(bool(b.get('started_at')) for b in c.get('batches',[])))


def execution_progress(task):
    if task.get('historical'):
        # CLI archives can have unlimited/absent execution budgets. They are
        # read-only evidence, never work for the web scheduler to allocate.
        return {'known_entries':0,'known_attempted':0,'known_read':0,'known_pending':0,
                'snippet_calls':0,'snippet_call_limit':0,'snippet_reserve_released':False,
                'withheld_records':0,'original_calls':0}
    from .search_execution import search_unavailable
    resources=[c for c in task['candidates'] if c.get('resource_provenance')]
    packets=[c for c in task['candidates'] if c.get('evidence_packet')]
    packet_calls=sum(batch_calls(c) for c in packets)
    released=(task['usage']['pages']>=task['input']['page_limit'] or search_unavailable(task)) and not any(c.get('page_snapshot') and not c.get('evidence_packet') and c['status'] in ('pending','pending_extract','retry_pending','reading') for c in task['candidates'])
    packet_limit=max(0,task['input']['model_call_limit']-task['usage']['model_calls']+packet_calls) if released else max(1,task['input']['model_call_limit']//5)
    return {'known_entries':len(resources),'known_attempted':sum(bool(c.get('read_attempts')) for c in resources),
        'known_read':sum(c['status']=='read' for c in resources),'known_pending':sum(c['status'] in ('pending','pending_extract','retry_pending') for c in resources),
        'snippet_calls':packet_calls,'snippet_call_limit':packet_limit,'snippet_reserve_released':released,
        'withheld_records':sum(len(c.get('rejected_records',[])) for c in task['candidates']),
        'original_calls':sum(batch_calls(c) for c in task['candidates'] if not c.get('evidence_packet'))}


def choose_candidate(task):
    from .site_flow import candidate_available
    pending=[c for c in task['candidates'] if c['status'] in ('pending','pending_extract','retry_pending') and ready(c)]
    pending=[c for c in pending if candidate_available(task,c)]
    pending=[c for c in pending if c.get('page_snapshot') or task['usage']['pages']<task['input']['page_limit']]
    progress=execution_progress(task)
    packets=[c for c in pending if c.get('evidence_packet')]
    originals=[c for c in pending if not c.get('evidence_packet')]
    resources=[c for c in originals if c.get('resource_provenance')]
    resource_calls=sum(batch_calls(c) for c in task['candidates'] if c.get('resource_provenance'))
    resource_reads=sum(c.get('read_attempts',0) for c in task['candidates'] if c.get('resource_provenance'))
    protected=[c for c in resources if (c.get('page_snapshot') or resource_reads<max(1,task['input']['page_limit']*3//10))]
    if protected and resource_calls<max(1,task['input']['model_call_limit']*3//10):
        # Previously successful details precede menus. Rotate companies and
        # batches; one very long page cannot starve other known entrances.
        def key(c):
            p=c['resource_provenance'];company=p.get('company')
            attempted=sum(x.get('read_attempts',0) for x in task['candidates'] if (x.get('resource_provenance') or {}).get('company')==company)
            return (not p.get('successful_detail',False),attempted,batch_calls(c),c.get('preferred_reader')!='browser')
        return min(protected,key=key)
    usable_packets=packets if progress['snippet_calls']<progress['snippet_call_limit'] else []
    if usable_packets and (not originals or progress['snippet_calls']<max(1,progress['original_calls']//3)):
        return min(usable_packets,key=batch_calls)
    if originals:
        counts={}
        for c in task['candidates']:
            if c.get('evidence_packet'):continue
            group=(c['channel'],c['city'])
            counts[group]=counts.get(group,0)+c.get('read_attempts',0)+batch_calls(c)
        return min(originals,key=lambda c:(not c.get('priority_recovery',False),
            counts.get((c['channel'],c['city']),0),evidence_priority(task,c),c.get('selection',{}).get('priority',3),batch_calls(c)))
    return None
