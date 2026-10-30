# JobFinder Agent

中国大陆岗位检索本机工作台。版本 0.3.0-beta.1 / Beta / MIT。网页、Python 后台、任务恢复和 Excel 导出独立运行，不需要 Codex 或其他 agent。

Windows 安装：准备 Python 3.11+ 和 Microsoft Edge。源码用户还需要 Node.js 22.12+（建议 24）及 pnpm 11.25.0。

```powershell
powershell -ExecutionPolicy Bypass -File scripts/setup.ps1
```

如果拿到的是包含 web/dist 的预构建 Release ZIP，可加 -SkipFrontend，无需 Node/pnpm。setup 在项目内建立 .venv。安装后双击 启动JobFinder.cmd；停止用 停止JobFinder.cmd。服务地址 http://127.0.0.1:8765。

进入“连接设置”，保存并测试自己的模型 API。默认使用 Edge 浏览器搜索；模型联网和 Tavily 可选，费用依服务商。密钥只保存在本机；仓库没有预置账号和搜索记录。

先展开岗位范围，再搜原始来源；匹配由核心规则计算。可导出十列 Excel 和可再次导入的结果 JSON。验证码或登录需要手工完成，不保证任意站点可访问，不自动投递。

完整说明见 [WEBAPP.md](WEBAPP.md)、[DATA_SCHEMA.md](DATA_SCHEMA.md)。MIT 许可与第三方说明见 [LICENSE](LICENSE)、[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。data/webapp 和个人配置由忽略规则排除，不要强行加入 Git。

验证命令：安装依赖并构建前端后运行 python scripts/smoke_release.py。自动化测试使用临时空数据目录，不需要用户密钥。使用者真实 API、平台登录和实时搜索还需在自己的环境完成首次验证。
