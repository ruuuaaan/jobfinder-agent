# 本机 Agent 使用

适用 Windows。运行后使用浏览器操作，后台不依赖 Codex 或其他 agent。实时任务需要用户自己的模型 API、网络访问；默认浏览器搜索使用 Microsoft Edge。模型联网与 Tavily 是可选搜索方式，不保证每个模型接口都支持联网。

## 安装

1. 安装 Python 3.11+（建议 3.12）并确保可从终端调用。
2. 从 GitHub 源码安装时还需要 Node.js 22.12+（建议 24）及 pnpm 11.25.0。
3. 在项目目录运行 `powershell -ExecutionPolicy Bypass -File scripts/setup.ps1`。该参数只作用于这次进程，不修改系统执行策略。
4. 若使用含 web/dist 的预构建发布 ZIP，可加 `-SkipFrontend`，不需要 Node/pnpm。
5. 没有 PATH 中的 Python 时使用 `-Python "解释器完整路径"`。依赖安装到项目 .venv，不修改全局 Python。

先用 setup 验证依赖和时区；浏览器采集使用系统 Microsoft Edge，无须下载另一个 Playwright 浏览器。首次启动在“连接设置”中填写、保存并测试自己的模型服务。

## 启动与停止

双击 启动JobFinder.cmd，打开 http://127.0.0.1:8765。也可运行 scripts/start-jobfinder.ps1 -Port 8765。脚本优先使用本项目 .venv；明确提供 -Python 时用指定解释器。

双击 停止JobFinder.cmd 或运行 scripts/stop-jobfinder.ps1。只停止本项目记录的服务及其 worker，不批量关闭其他 Python 程序。关闭页面不停止后台，电脑重启后需重新启动。

数据默认存放 data/webapp。JOBFINDER_WEB_DATA 可指定另一绝对目录，启动和停止必须使用同一设置；同时运行多份时也需要不同端口。

## 使用流程

先填写城市和岗位，查看并修正展开后的岗位词，再确认预算。我的学历、经验、薪资、类型为可选条件。创建后才开始任务；搜索失败也计预算。登录或验证码由用户在专用 Edge 窗口完成。

“岗位结果”可导出十列 Excel。选定一个任务后可导出结果 JSON；“导入结果 JSON”支持本机版和 Skill 的标准结果包，保留原时间和结论，不会重新搜索。

## 版本和已知边界

0.3.0-beta.1；新任务规则 CN_MAINLAND-4.0。这是个人本机应用，不用于公网多人部署。受限网站、动态页面和模型接口差异会导致部分完成或待确认；不承诺任意站点自动适配，不自动投递。

日志位于所选数据目录 server.out.log / server.err.log。不要把含个人资料的日志或结果直接上传 GitHub。
