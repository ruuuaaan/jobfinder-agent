"""CLI boundaries for the persistent library and original announcement discovery."""
import json
from .models import Candidate, now
from .resources import ResourceStore, record_candidates, record_task_companies
from .announcements import read_announcement
from .network import Fetcher, FetchError
from .planning import authorize_config, validate_receipt

COMMANDS = ('resources', 'resource-import', 'record-discovery', 'read-announcement', 'record-company-facts')


def register(commands):
    for name in COMMANDS:
        p = commands.add_parser(name)
        if name in ('resource-import', 'record-discovery', 'record-company-facts'):
            p.add_argument('--input', required=True, help='企业候选JSON；只记录发现，不把摘要提升为已核实')
        if name in ('record-discovery', 'read-announcement'):
            p.add_argument('--config', required=True)
            p.add_argument('--allow-example', action='store_true')
        if name == 'record-discovery':
            p.add_argument('--plan', required=True)
        if name == 'read-announcement':
            p.add_argument('--url', required=True)
            p.add_argument('--company', action='append', help='多企业公告只反查指定全称，可重复；省略时记录所有识别企业')
            p.add_argument('--policies', required=True)
            p.add_argument('--output', required=True)


def execute(args):
    from .service import ROOT, local_path, output_path, load_config
    if args.command == 'record-company-facts':
        from .company_facts import CompanyFacts, record_facts
        batch = CompanyFacts.model_validate_json(local_path(args.input).read_text(encoding='utf-8-sig'))
        if batch.artifact_kind != 'user':
            raise ValueError('示例资料不得写入真实企业库')
        store = ResourceStore(ROOT/'data/resources/companies.sqlite3')
        ids = record_facts(store, batch)
        store.snapshot()
        print(json.dumps({'companies_recorded':len(ids),'library':str(store.path)},ensure_ascii=False))
        return 0
    store = ResourceStore(ROOT/'data/resources/companies.sqlite3')
    if args.command in ('resource-import', 'record-discovery'):
        data = json.loads(local_path(args.input).read_text(encoding='utf-8-sig'))
        if data.get('mode') == 'offline':
            raise ValueError('离线数据不得写入真实企业资源库')
        candidates = [Candidate.model_validate(c) for c in data['candidates']]
        tasks = data.get('search_log', [])
        if args.command == 'record-discovery':
            config = load_config(args.config)
            authorize_config(config, args.allow_example)
            plan = json.loads(local_path(args.plan).read_text(encoding='utf-8-sig'))
            tasks = validate_receipt(config, plan, data)
        ids = record_candidates(store, candidates, 'search_candidate' if args.command == 'record-discovery' else 'historical_candidate_import')
        ids += record_task_companies(store, tasks, 'historical_enterprise_query' if args.command == 'resource-import' else 'known_enterprise_query')
        ids = list(dict.fromkeys(ids))
        result = store.snapshot()
        print(json.dumps({'companies_recorded': len(ids), 'total_companies': len(result['companies']), 'library': str(store.path)}, ensure_ascii=False))
        return 0
    if args.command == 'resources':
        result = store.snapshot()
        print(json.dumps({'companies': [{k:v for k,v in c.items() if k not in ('observations','discoveries')} for c in result['companies']], 'library': str(store.path)}, ensure_ascii=False))
        return 0
    config = load_config(args.config)
    authorize_config(config, args.allow_example)
    output = output_path(args.output, 'real')
    policies = json.loads(local_path(args.policies).read_text(encoding='utf-8-sig'))
    fetcher = Fetcher(config.budget, policies, output/'evidence')
    report = {'operation': 'announcement_employer_discovery', 'source_url': args.url, 'started_at': now(),
              'companies': [], 'status': 'not_run', 'jobs_extracted': 0,
              'note': '本命令只发现企业；公告日期、当前招聘和岗位条件须另外核实'}
    try:
        companies, page = read_announcement(fetcher, args.url, store, args.company)
        for company in companies:
            company['target_cities'] = config.target_cities
        report.update(status='success' if companies else 'employer_not_parsed', companies=companies,
                      source_hash=page['hash'], snapshot=page['snapshot'], retrieved_at=page['retrieved_at'],
                      total_employers_on_page=page['employers_found'], selected_company_names=args.company)
    except (FetchError, ValueError, OSError) as exc:
        report.update(status=getattr(exc, 'status', 'failed'), reason=str(exc))
    (output/'companies.json').write_text(json.dumps({'companies': report['companies'], 'scope': '单次公告企业发现，不是完整企业目录'}, ensure_ascii=False, indent=2), encoding='utf-8')
    (output/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    (output/'network.jsonl').write_text(''.join(json.dumps(e, ensure_ascii=False)+'\n' for e in fetcher.events), encoding='utf-8')
    store.snapshot()
    print(json.dumps({'status': report['status'], 'companies': len(report['companies']), 'output': str(output)}, ensure_ascii=False))
    return 0 if report['status'] == 'success' else 4
