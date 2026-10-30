"""Apply observed closed-page evidence only to that vacancy, including later leads."""
from urllib.parse import urlsplit, parse_qsl, urlencode


def identity(url):
    p=urlsplit(url or '')
    query=[(k,v) for k,v in parse_qsl(p.query,keep_blank_values=True)
           if not k.startswith(('utm_','refer__')) and k not in ('rc','refcode','srccode','actionid','preactionid')]
    # Fragment routes and alternate ports can identify different vacancies.
    # A false negative is preferable to closing another job at the same path.
    return (p.netloc.casefold(),p.path.rstrip('/'),urlencode(sorted(query)),p.fragment)


def apply_closed_evidence(task):
    closed={identity(url):c['closed_page'] for c in task['candidates'] if c.get('closed_page')
            for url in c['closed_page']['urls']}
    for j in task['jobs']:
        ev=closed.get(identity(j.get('detail_url'))) or closed.get(identity(j.get('source_url')))
        if not ev:continue
        j['open_status']='CLOSED';j['validity_checked_at']=ev['retrieved_at']
        evidence={'source_url':ev['url'],'retrieved_at':ev['retrieved_at'],'content_hash':ev['content_hash'],
                  'method':'original_closed_notice','snapshot':ev['snapshot'],'field_quotes':{'open_status':ev['quote']},'untrusted_content':True}
        if evidence not in j.setdefault('evidence',[]):j['evidence'].append(evidence)
        note='原岗位页面明确显示已过期、关闭或不存在；不采用页面推荐的其他岗位作为本岗位详情'
        if note not in j.setdefault('notes',[]):j['notes'].append(note)
