import hashlib
from .storage import ROOT

SKILL = ROOT/'rules/jobfinder.md'


def current_rules():
    text=SKILL.read_text(encoding='utf-8-sig')
    return {'path':str(SKILL),'sha256':hashlib.sha256(text.encode()).hexdigest(),'text':text}


def snapshot_rules(store,task):
    rules=current_rules()
    path=store.path/'rules'/task['id']/'SKILL.md'
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(rules['text'],encoding='utf-8')
    return store.update(task['id'],lambda t:t.update(skill_sha256=rules['sha256'],skill_snapshot=str(path)))


def model_rules(task=None):
    from pathlib import Path
    text=Path(task['skill_snapshot']).read_text(encoding='utf-8') if task and task.get('skill_snapshot') else current_rules()['text']
    # Full rules are retained; only relevant sections enter each model call.
    sections=[]
    for section in text.split('\n## '):
        if any(word in section.split('\n')[0] for word in ('岗位展开','公告','边界','证据')):
            sections.append(section)
    return '\n\n只理解岗位范围并提取原文事实；不得判断学历、经验、地点是否符合，不得返回 MATCH / REVIEW / EXCLUDED。最终判断只由 jobfinder.matching.evaluate 执行，输入条件只认 effective_search_spec。\n'+('\n## '.join(sections))[:18000]
