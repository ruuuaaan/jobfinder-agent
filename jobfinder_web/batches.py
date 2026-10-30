"""Resumable overlapping source spans. Each invocation consumes one model call."""
import hashlib


def span(start,end,parent=None):
    return {'id':hashlib.sha256(f'{start}:{end}:{parent}'.encode()).hexdigest()[:16],
            'start':start,'end':end,'status':'pending','parent':parent}


def plan(text,size=1600,overlap=400):
    result=[];start=0
    while start<len(text):
        end=min(len(text),start+size)
        # Preserve complete clauses when possible; never drop intervening text.
        if end<len(text):
            cut=max(text.rfind(mark,start+size//2,end) for mark in ('。','；','\n',' '))
            if cut>start+size//2:end=cut+1
        result.append(span(start,end))
        if end==len(text):break
        start=max(start+1,end-overlap)
    return result or [span(0,0)]


def split(batch):
    if batch['end']-batch['start']<=650:return []
    middle=(batch['start']+batch['end'])//2
    return [span(batch['start'],middle+150,batch['id']),span(middle-150,batch['end'],batch['id'])]


def progress(candidate):
    leaves=[b for b in candidate.get('batches',[]) if b['status']!='split']
    return {'total':len(leaves),'done':sum(b['status']=='done' for b in leaves),'failed':sum(b['status']=='failed' for b in leaves)}
