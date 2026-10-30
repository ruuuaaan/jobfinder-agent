import argparse
import json
import sys
from pathlib import Path
from .service import ROOT, load_config, run, reexport, local_path
from .catalog import import_catalog, save_new_json, import_official_directory
from .models import SearchConfig
from .planning import build_plan, effective_config, authorize_config, extend_plan


def main():
    parser=argparse.ArgumentParser(prog="python -m jobfinder",description="中国大陆岗位检索：Agent辅助发现 + 独立可测试核心")
    commands=parser.add_subparsers(dest="command",required=True)
    for name in ("validate-config","offline","process-candidates","process-browser","search","export","plan","extend-plan","new-search","refresh","resume-traversal"):
        p=commands.add_parser(name)
        p.add_argument("--config",required=True)
        p.add_argument("--city",action="append",help="覆盖目标城市，可重复；不回退到示例")
        p.add_argument("--role",action="append",help="覆盖岗位关键词，可重复；不自动扩展同义词")
        if name!="validate-config":
            p.add_argument("--output",required=True)
            p.add_argument("--allow-example",action="store_true")
        if name in ("offline","process-candidates","new-search","refresh"):
            p.add_argument("--candidates",required=True)
        if name in ("process-candidates","new-search","refresh","resume-traversal"):
            p.add_argument("--policies")
        if name in ("process-candidates","new-search","refresh","plan","extend-plan"):
            p.add_argument("--catalog")
        if name=="new-search":
            p.add_argument("--plan",required=True,help="本次配置生成的查询计划")
        if name in ("export","extend-plan","resume-traversal","process-browser"):
            p.add_argument("--input",required=True)
        if name=='process-browser':
            p.add_argument('--role-reviews',help='助手依据原文填写的职能判断JSON；校验URL、原关键词、摘录与快照哈希')
        if name=='resume-traversal':
            p.add_argument('--budget-mode',choices=['remaining','new_run'],default='remaining',help='默认沿用剩余额度；new_run显式启用本轮配置的新一轮预算')
        if name in ('plan','extend-plan'):
            p.add_argument('--role-groups',help='岗位词组与性质JSON；不改变原有岗位查询用词')
            p.add_argument('--company-assessments',help='助手给出的逐企业/城市/岗位组原文证据判断')
            p.add_argument('--knowledge-base',help='只读企业SQLite库或完整JSON快照')
            p.add_argument('--catalog-source',choices=['enterprise_directory','listed_companies','knowledge_base'])
            p.add_argument('--company',action='append',help='用户明确指定的企业，保留查询入口；可重复')
    p=commands.add_parser("import-companies")
    p.add_argument("--input",required=True)
    p.add_argument("--source-url",required=True)
    p.add_argument("--version-date",required=True)
    p.add_argument("--output",required=True)
    p.add_argument("--mapping")
    p.add_argument("--header-row",type=int,default=1)
    p.add_argument("--listed-default",choices=["true","false"])
    p.add_argument("--previous")
    p=commands.add_parser('import-directory',help='导入有权保存的政府企业目录原始HTML，不声称在线读取')
    p.add_argument('--input',required=True)
    p.add_argument('--source-url',required=True)
    p.add_argument('--scope',required=True)
    p.add_argument('--list-date')
    p.add_argument('--limit',type=int,help='仅显式抽样时使用；默认解析本地页面全部企业后再筛选')
    p.add_argument('--output',required=True)
    from .resource_cli import register, execute, COMMANDS
    register(commands)
    args=parser.parse_args()
    try:
        if args.command in COMMANDS:
            return execute(args)
        if args.command=='import-directory':
            if args.limit is not None and not 1<=args.limit<=100:
                raise ValueError('目录样本数量必须1—100')
            result=import_official_directory(local_path(args.input),args.source_url,args.scope,args.list_date,args.limit)
            save_new_json(local_path(args.output),result)
            print(json.dumps({'companies':len(result['companies']),'mode':result['mode']},ensure_ascii=False))
            return 0
        if args.command=="import-companies":
            mapping=json.loads(local_path(args.mapping).read_text(encoding="utf-8")) if args.mapping else None
            previous=json.loads(local_path(args.previous).read_text(encoding="utf-8")) if args.previous else None
            result=import_catalog(local_path(args.input),args.source_url,args.version_date,mapping,args.header_row,None if args.listed_default is None else args.listed_default=="true",previous)
            save_new_json(local_path(args.output),result)
            print(json.dumps({"companies":len(result["companies"]),"output":str(local_path(args.output))},ensure_ascii=False))
            return 0
        raw=json.loads(local_path(args.config).read_text(encoding="utf-8-sig"))
        overrides={}
        if args.city is not None:
            overrides["target_cities"]=args.city
        if args.role is not None:
            overrides["role_keywords"]=args.role
            overrides["role_mode"]="keywords"
        from .spec import matching_config
        config=matching_config(raw)
        if overrides:
            if raw.get('schema_version')=='job-search-spec-1':
                import copy
                adjusted=copy.deepcopy(raw)
                if 'target_cities' in overrides:
                    adjusted['target_locations']=overrides['target_cities']
                    old=adjusted.get('search_budget',{}).get('per_city',{})
                    adjusted.setdefault('search_budget',{})['per_city']={c:old.get(c,config.budget.searches_per_city) for c in overrides['target_cities']}
                if 'role_keywords' in overrides:adjusted['job_scope'].update(mode='keywords',terms=overrides['role_keywords'])
                config=matching_config(adjusted)
            else:config=SearchConfig.model_validate(config.model_dump(mode='json') | overrides)
        effective=effective_config(config,args.config,overrides)
        effective["raw_input"]={"config":raw,"overrides":overrides}
        print(json.dumps({"effective_config":effective},ensure_ascii=False))
        if args.command=="validate-config":
            print(config.model_dump_json(indent=2))
            return 0
        authorize_config(config,args.allow_example,args.command=="offline")
        if args.command in ('plan','extend-plan'):
            from .resources import ResourceStore
            def read_json(path):
                return json.loads(local_path(path).read_text(encoding='utf-8-sig')) if path else None
            catalog=read_json(args.catalog)
            if args.catalog_source and catalog is not None:
                catalog['source_kind']=args.catalog_source
            knowledge=None
            if args.knowledge_base:
                path=local_path(args.knowledge_base)
                knowledge=read_json(path)['companies'] if path.suffix.lower()=='.json' else ResourceStore.read_existing(path)
            selection_args=dict(role_groups=read_json(args.role_groups),company_assessments=read_json(args.company_assessments),
                                knowledge=knowledge,requested_companies=args.company or [])
            default_library=ROOT/'data/resources/companies.sqlite3'
            selection_args['entry_knowledge']=knowledge if args.knowledge_base else ResourceStore.read_existing(default_library) if default_library.exists() else []
        if args.command=="extend-plan":
            if not args.catalog and not args.knowledge_base and not args.company:
                raise ValueError('扩展需要 --catalog、--knowledge-base 或用户明确指定 --company')
            plan=extend_plan(config,json.loads(local_path(args.input).read_text(encoding='utf-8')),catalog,**selection_args)
            save_new_json(local_path(args.output),plan)
            print(json.dumps({'status':'awaiting_entry_reads_and_search','tasks':plan['tasks'],'entry_tasks':plan.get('entry_tasks',[])},ensure_ascii=False))
            return 0
        if args.command=="plan":
            plan=build_plan(config,catalog,**selection_args)
            plan["input_provenance"]=effective
            save_new_json(local_path(args.output),plan)
            print(json.dumps({"plan_id":plan["plan_id"],"tasks":plan["tasks"],"entry_tasks":plan.get('entry_tasks',[]),"status":"awaiting_entry_reads_and_search","output":str(local_path(args.output))},ensure_ascii=False))
            return 0
        if args.command=='process-browser':
            from .browser import process_browser_run
            result=process_browser_run(config,local_path(args.input),local_path(args.output),ROOT/'data/resources/companies.sqlite3',role_reviews_path=local_path(args.role_reviews) if args.role_reviews else None)
        elif args.command=="export":
            result=reexport(config,args.input,args.output,effective)
        elif args.command=='resume-traversal':
            result=run(config,args.output,policies_file=args.policies,operation='resume_traversal',effective=effective,
                       resource_path=ROOT/'data/resources/companies.sqlite3',resume_file=args.input,resume_budget=args.budget_mode)
        else:
            operation={"new-search":"new_search","refresh":"refresh"}.get(args.command,"provided_links")
            result=run(config,args.output,getattr(args,"candidates",None),getattr(args,"policies",None),"test" if args.command=="offline" else "real",getattr(args,"catalog",None),operation,getattr(args,"plan",None),effective,resource_path=ROOT/"data/resources/companies.sqlite3" if args.command!="offline" else None)
        print(json.dumps({"mode":result["mode"],"jobs_extracted":0 if args.command=='export' else len(result['jobs']),"jobs_in_evidence":len(result["jobs"]),"counts":result["counts"],"discovery":result["discovery"]["status"],"output":str(local_path(args.output))},ensure_ascii=False))
        return 3 if args.command=="search" and result["discovery"]["status"]=="unconfigured" else 0
    except (ValueError, OSError, KeyError) as exc:
        print("JobFinder错误："+str(exc),file=sys.stderr)
        return 2


if __name__=="__main__":
    raise SystemExit(main())
