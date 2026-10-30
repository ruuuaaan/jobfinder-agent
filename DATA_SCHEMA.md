# 数据与导出契约

软件版本 0.3.0-beta.1；有效条件 schema_version 为 job-search-spec-1；新任务业务规则为 CN_MAINLAND-4.0。结果包字段及旧规则兼容以 jobfinder/spec.py、models.py、exporter.py 为准。

## 条件与证据

raw_input 保留原输入，effective_search_spec 保存实际生效条件；不能用提示词重新解释本人学历、本人经验、岗位门槛或工作地点。config 是旧核心消费的兼容投影。

Job 保存来源、岗位原文、具体工作地点、学历与经验原文、薪资、开放状态及 evidence。证据含 source_url、retrieved_at、content_hash、method、snapshot、field_quotes。缺失字段保持未知。

MATCH / REVIEW / EXCLUDED 只能由匹配核心产生；checked_at 是计算时间，retrieved_at/validity_checked_at 是原始证据时间，不得混用。手工导入、列表卡片、搜索摘要与完整原文来源各自标明。

## 用户工作簿

岗位汇总.xlsx 为一个工作表，顺序固定：

| 列 | 内容 |
|---|---|
| A | 公司名称 |
| B | 岗位名称 |
| C | 岗位描述 |
| D | 薪资水平 |
| E | 投递链接 |
| F | 工作地点 |
| G | 学历要求 |
| H | 经验要求 |
| I | 岗位来源 |
| J | 待确认说明 |

默认包含 MATCH 与 REVIEW；EXCLUDED 仍保留在结果 JSON 与工程输出。链接使用原岗位/投递入口；未知薪资、地点等不补造。工作簿保留筛选和冻结首行。

## 可移植历史结果

本机版“导出结果 JSON”保存 input、config、effective_search_spec、raw_input、results。Skill 生成的 results.json 也可以导入。需有原始条件、核验时间和完整结果结构；不接受只有表格行而没有条件的 JSON。

导入按文件内容去重，不联网、不重新匹配；原结果与时间保持不变。最多 10 MiB / 5000 条；较大任务可分别导出。合并结果保留来源，不能将旧证据当成本次核验。

## 存储边界

真实任务使用 outputs/real，合成验收使用 outputs/test。本机后台数据默认 data/webapp，可用 JOBFINDER_WEB_DATA 指定独立目录。配置、凭据、浏览器状态和实际收集数据不进入发布包。
