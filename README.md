# MathAgent

本地数学研究工作台，提供图与工作稿、不可变版本、证明依赖、证据与采用、批注、冲突处理，以及独立 HTTP worker。研究任务可以在额度内读取材料、计算、提出论证、派生子任务和请求独立审查；结果仍需人工决定是否采用。默认使用 FakeProvider。

最初缺口见 [蓝图落实审计](docs/acceptance/blueprint-audit-2026-09-08.md)，最新结果见 [一期补齐、研究地图与 IMO 准入评估](docs/acceptance/completion-assessment-2026-09-08.md)。功能与浏览器回归已完成；真实主任务收尾仍有协议失败记录，未宣称整个阶段已放行。

当前工作：[IMO-AnswerBench 四题无人试测](docs/evaluation/imo-answerbench-pilot.md)。固定四领域各一题，解题时不读取答案、不开放网页检索；结果和独立离线评分分别保存。自动文献检索已列入蓝图后续范围，本轮不实现。

2026-09-13 补齐了分支三方合并、永久删除与文章整理，并接入默认关闭的离线代码沙箱；同时修复长证明审查材料送达和审查恢复额度。沙箱真实隔离验收仍被本机 Windows 虚拟化环境阻塞，因此尚未启动新的数论复跑。详见 [本轮复核与边界](docs/acceptance/blueprint-recheck-2026-09-13.md) 和 [沙箱准备](sandbox/README.md)。

## 启动工作台

需要 Python 3.13、uv、Node.js 22 或更新版本。在项目根目录运行 PowerShell：

```powershell
.\scripts\bootstrap.ps1
.\scripts\dev.ps1
```

打开 [本地工作台](http://127.0.0.1:8000)。启动脚本构建网页并启动 API 和两个并发槽的模拟 worker；Ctrl+C 停止它启动的进程。`-SkipBuild` 复用已有构建，`-NoWorker` 只操作人工工作台，`-Port 8001` 更换端口。

Python 使用项目 `.venv`，依赖由 `uv.lock` 固定；前端由 `apps/web/package-lock.json` 固定。需要指定解释器时：`./scripts/bootstrap.ps1 -Python 'C:\Python313\python.exe'`。

研究数据在 `data/mathagent.db`，服务日志按端口保存到 `data/logs/<端口>/`。第一次启动自动迁移数据库并生成独立用户/worker 令牌。网页通过仅限本机的 HttpOnly 会话进入，写操作检查来源与自定义请求头；密钥和数据库不提交到 Git。令牌文件供本机 API 工具使用，勿粘贴到研究正文。

## 一次完整的本地操作

1. 新建研究，保存原问题。也可打开“特征值反例示例”，查看有明确来源的精确计算和衍生问题。
2. 添加命题、上下文或笔记；点击图节点或工作稿正文查看详情。正文支持 Markdown 和 LaTeX。未提交的对象编辑暂存于当前浏览器会话。
3. 在命题的“论证”页添加证明方案，并选择共同前提、上下文和暂时假设。方案之间可以替代，方案内共同前提必须全部满足。
4. 分别记录审查和人工采用。支持状态只表示明确证据策略下的结果，不能把人工采用、数值实验或模型输出自动当作证明。
5. 启动研究后，在运行页查看实际执行、请求账本，或暂停、插话、停止。在运行过程中保存引理新版本，旧输入和旧产物仍可追溯；过时结果进入候选分支。
6. 用分支选择器查看候选材料，用版本页比较旧正文，用冲突处理区明确选择候选或当前内容。搜索可以找到旧版和旁支，搜索结果会标明来源。
7. 导出固定快照的工作稿、证据和产物；原问题不会因新增衍生问题而消失。

地图按依赖方向排列，用端口和箭头连接节点；选中节点会突出相邻路径。可以拖动、聚焦、重新整理，也可折叠路线或隐藏对象；布局与数学依赖分别保存。SSE 断线后从事件游标补发，保存布局期间保留正在拖动的位置。

数学正文使用 LaTeX：行内写 `$...$`，独立公式写 `$$...$$`；图卡片、工作稿、审查及历史版本均渲染公式。代码块仍按原文显示。旧材料中没有公式分隔符的文本需要编辑成 LaTeX，系统不会猜测并改写原有数学内容。

自主研究默认最多八步、两轮审查、四个后代任务。子任务、格式修复和重试共同占用祖先任务、分支及项目的请求额度；达到步数或额度上限后，可调整上限再恢复。运行页保留每步操作回执、实际模型配置、原始输出及截断标记；项目操作权限与真实模型许可分别设置。

## 真实 API 联调入口

自动测试不调用付费 API。2026-09-08 已完成一次接入冒烟，以及两批共 11 次真实功能验收请求；第二批通过计算、证明保存、独立审查和在途改题恢复，主任务最终输出违反角色协议被拒收。完整结果和后续修复见 [验收评估](docs/acceptance/completion-assessment-2026-09-08.md)，不将简单题流程演示当作 IMO 能力测试。真实调用需要同时满足：本机总开关、密钥和模型名称、本项目许可、允许的提供方、显式启动的对应 worker、剩余请求额度。

在项目根目录复制 `.env.example` 为 `.env`，取消所需变量前的注释并在本机填写。API 和 worker 启动时会读取各自工作目录下的 `.env`；使用 `dev.ps1` 时两者都从项目根目录读取。已有进程环境变量优先，修改后需要重启。支持 Windows 常见的 UTF-8（含 BOM）和带 BOM 的 UTF-16 文件；值按原文读取，不执行命令或变量替换。

```dotenv
# .env 中的 DeepSeek 配置；模型名使用账户当前可用名称。
MATHAGENT_DEEPSEEK_API_KEY=<本机填写>
MATHAGENT_DEEPSEEK_MODEL=<模型名称>
MATHAGENT_ENABLE_REAL_API=1
```

然后在 PowerShell 启动对应 worker：

```powershell
.\scripts\dev.ps1 -Providers fake,deepseek
```

也可继续在 PowerShell 中设置同名环境变量；已有空值和 `0` 也会覆盖 `.env`。若需完全跳过文件加载，启动前设置 `$env:MATHAGENT_LOAD_ENV='0'`。自动测试、固定演示和性能测试会关闭文件加载及真实调用。

GLM 对应变量为 `MATHAGENT_GLM_API_KEY` 和 `MATHAGENT_GLM_MODEL`，启动参数为 `-Providers fake,glm`。默认服务地址分别为 `https://api.deepseek.com` 和 `https://open.bigmodel.cn/api/paas/v4`；只在需要账户支持的其他地址时更改 `MATHAGENT_<PROVIDER>_BASE_URL`。

打开运行页的“运行设置”，显式允许当前项目使用所选真实提供方。首次联调建议新建一个不含私有资料的项目，项目上限设为 2 次、单任务上限 1 次：先验证候选研究输出，再对明确的论证版本发起独立审查。检查调用编号、实际 usage、输入版本与结果，不把预置示例冒充模型自主发现。

账本的额度单位是**请求次数**，并不保证人民币/美元费用上限。预留、已发出、已使用和结果不明都会占用额度。连接中断后的结果不明请求不会盲目重发；核对提供方记录后，在界面登记对账依据，再手动恢复任务。

可选的“保留占用，原预算内续试一次”策略允许传输中断后追加一次调用：旧记录保持 `unknown`，主任务与全部子任务共享一次授权，并继续受各级请求预算约束。默认关闭；暂停、取消、输入变化、租约失效或再次结果不明时停止。数学工作流完成不等于费用已对账。

## IMO-AnswerBench 无人试测

这会调用真实模型；默认四题、每题最多 12 次请求、两题并行、Think Max、每请求最多 65,536 输出 tokens / 600 秒、每题最多 1,800 秒。子任务、审查和格式修复共享每题额度。模型名显式使用 `deepseek-v4-flash`，不改 `.env`。

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts\imo_answerbench.py --execute --output data\benchmarks\answerbench-four-run-01
.\.venv\Scripts\python.exe -X utf8 scripts\score_answerbench.py --batch-dir data\benchmarks\answerbench-four-run-01
```

第一条只打开题面文件；第二条在解题全部结束后读取独立答案键，以保守符号等价规则评分。它不是官方 Gemini AnswerAutoGrader，也不对证明质量评分。四题成绩不能代表完整题集准确率。

中断后对同目录加 `--resume`，保持模型、参数、题面、源码及依赖锁不变。已完成题不会重复调用；已经发送但结果不明的请求不会盲重试。源码/参数变更后的复测必须用新目录，保留原批次。每题目录包含独立数据库、步骤、调用参数与可见输出、实际请求 payload（不含鉴权头）、导出和最终状态。

## 备份与恢复

备份使用 SQLite 在线备份接口，可以在服务运行期间执行。输出文件不能已存在：

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts\backup.py --database data\mathagent.db --output backups\research-2026-09-08.zip
.\.venv\Scripts\python.exe -X utf8 scripts\restore.py --backup backups\research-2026-09-08.zip --destination data\restored-research
```

恢复目标必须是新的或空目录；恢复前校验文件大小、哈希和数据库完整性。备份携带数据库引用的固定计算文件，不包含会话令牌；活动运行会标记为中断，不会自动启动 worker。查看恢复报告后，在另一个端口打开：

```powershell
$env:MATHAGENT_DATABASE = 'data\restored-research\mathagent.db'
.\scripts\dev.ps1 -Port 8001 -NoWorker -SkipBuild
```

备份仍包含研究正文，应按研究材料管理。SQLite 数据目录必须位于本机磁盘。

## 验证与开发

```powershell
.\.venv\Scripts\python.exe -X utf8 -m pytest
.\.venv\Scripts\ruff.exe check services tests scripts
.\.venv\Scripts\python.exe -X utf8 scripts\demo.py
npm.cmd --prefix apps/web run build
npm.cmd --prefix apps/web run test:e2e
.\.venv\Scripts\python.exe -X utf8 scripts\export_contracts.py
```

浏览器测试使用临时数据库、独立测试 worker 和本机 Chrome，固定测试端口 18081；使用 Edge 时先设 `$env:MATHAGENT_TEST_BROWSER='msedge'`。测试不会连接真实模型。截图与 trace 在 `apps/web/test-results/`，验收记录在 `docs/acceptance/`。WebMCP 为可选能力，注入注册器的契约测试与真实浏览器扩展支持分别记录。

需要前端热更新时，先在项目根目录设置 `$env:MATHAGENT_FRONTEND_ORIGIN='http://127.0.0.1:5173'` 并直接运行 uvicorn，再在 `apps/web` 执行 `npm.cmd run dev`。Vite 的 `/api` 代理指向 8000；生产构建由本地 API 直接提供。

[接口文档](http://127.0.0.1:8000/docs) 使用用户令牌授权，worker 接口要求独立 worker 令牌。写操作要求 `Idempotency-Key`；重复同一键/请求返回原回执，重复键配不同请求返回 409。更新对象必须带 `expected_revision_id`，更新笔记必须带 `expected_version` 与 `expected_body`。

## 能力边界

- 支持计算只覆盖显式版本依赖，不自动判断自然语言等价性或数学真伪。新版本不继承旧审查，假设未解除时保持条件标记；有争议的材料保留具体记录。
- 研究操作经过结构、版本、权限和额度校验；允许有界的递归子任务及独立审查，不提供任意代码执行、网页检索或自动形式化验证。固定工具支持有理数运算、多项式恒等式和 Hermitian 交叉算例，证据只覆盖实际检查范围。
- 请求数预算、两个并发执行槽、租约与幂等回执已实现；DeepSeek 的实际工具操作、审查、干预隔离与 usage 已实测。GLM 账户尚未配置；真实外部超时、取消后计费和限流等场景仍需账户验证。普通自动测试中的故障注入不代表付费服务端计费已核对。
- 分支创建和版本冲突处理已提供；完整分支三方合并、永久删除、跨项目复用与文章级证明整理属于后续阶段。
- Python wheel 包含 API/worker；图形工作台从本仓库的 `apps/web/dist` 加载。分离安装时需另外构建前端并设置 `MATHAGENT_FRONTEND_DIST`。

施工依据：[施工方案](施工方案/数学科研多Agent工作台_施工方案_v0.1.md)；阶段证据：[M0—M1](docs/acceptance/m0-m1.md)。

离线阶段和后续联调进展见 [验收记录](docs/acceptance/api-readiness.md)。

IMO-AnswerBench 修复后真实模型复测应通过 `--case-id` 指定失败题，保留历史批次；代码回归使用本地模拟调用。定向执行、输出截断恢复及独立评分命令见 [试测记录](docs/evaluation/imo-answerbench-pilot.md)。

评测通过 `--unknown-recovery once` 显式启用上述策略，默认 `stop`；策略写入冻结配置，不能在原批次中途切换。恢复同一任务不会重置续试次数。另建独立批次时必须核对历史占用及已用续试，扣减新批次预算，已使用续试则选择 `stop`；不同批次的独立数据库不会自动合并额度。当前付费测试保持暂停。
