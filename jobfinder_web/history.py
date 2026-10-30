"""Portable result archives. Importing never re-evaluates or refreshes evidence."""
import copy
import hashlib
import json
from jobfinder.models import MatchResult, now
from jobfinder.spec import JobSearchSpec, spec_for_config
from .domain import CHANNELS, TaskInput, new_task

MAX_BYTES = 10 * 1024 * 1024


def archive(task, records):
    return {'schema_version': 'jobfinder-history-1', 'input': task['input'],
            'config': task['config'], 'effective_search_spec': task['effective_search_spec'],
            'raw_input': task.get('raw_input'), 'results': copy.deepcopy(records)}


def imported_task(data):
    if not isinstance(data, dict) or not isinstance(data.get('results'), list):
        raise ValueError('请选择 JobFinder 导出的结果 JSON，文件必须包含 results 数组')
    if not 1 <= len(data['results']) <= 5000:
        raise ValueError('历史结果需包含 1–5000 条记录')
    if data.get('effective_search_spec'):
        spec = JobSearchSpec.model_validate(data['effective_search_spec'])
    elif data.get('config'):
        spec = spec_for_config(data['config'])
    else:
        raise ValueError('结果缺少原始检索条件 config 或 effective_search_spec，不能猜测城市和岗位')
    records = copy.deepcopy(data['results'])
    for record in records:
        if not isinstance(record, dict) or 'checked_at' not in record:
            raise ValueError('结果缺少原始核验时间，无法作为历史记录导入')
        # Export-only fields such as salary_normalized stay intact.
        MatchResult.model_validate({k: v for k, v in record.items() if k in MatchResult.model_fields})
    cities = spec.target_locations
    role = '、'.join(spec.job_scope.terms) or '不限岗位'
    inp = TaskInput(cities=cities, role=role[:120], channels=[c for c in spec.channels if c in CHANNELS] or ['official_career'],
                    city_budgets={c: spec.search_budget.per_city.get(c, 10) for c in cities})
    task = new_task(inp)
    digest = hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    task.update(state='historical', historical=True, queries=[], saved_results=records,
                import_sha256=digest, jobs=[r['job'] for r in records],
                config=spec.to_config().model_dump(mode='json'), effective_search_spec=spec.model_dump(mode='json'),
                raw_input=copy.deepcopy(data.get('raw_input')), import_source='user_selected_json',
                events=[{'at': now(), 'message': '导入用户选择的结果，保留原核验时间和结论；未重新联网', 'kind': 'info'}])
    return task
