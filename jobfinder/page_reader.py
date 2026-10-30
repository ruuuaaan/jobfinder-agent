"""Bounded list-to-detail reading. HTML and script text never execute code."""
from .parsers import document


def dynamic_shell(data):
    doc=document(data)
    return bool(doc.xpath('//script[@src]')) and bool(doc.xpath('//*[@id="app" or @id="root" or contains(@class,"post_list") or contains(@class,"job_list")]'))


def extract_pages(fetcher, candidate, first_page, reports, base_report, page_limit=None, targets=None, config=None, state=None):
    """Traverse observed list pages and relevant/uncertain details until evidence or budget stops it."""
    from .traversal import walk
    return walk(fetcher,candidate,first_page,reports,base_report,config,state,page_limit,targets)


def extract_page_steps(fetcher, candidate, first_page, reports, base_report, page_limit=None, targets=None, config=None, state=None):
    from .traversal import walk_steps
    return walk_steps(fetcher,candidate,first_page,reports,base_report,config,state,page_limit,targets)
