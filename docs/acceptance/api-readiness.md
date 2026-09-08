# 真实 API 联调前验收记录

> 最新实现、浏览器与真实请求结果见[一期补齐与研究地图验收](completion-assessment-2026-09-08.md)。本文其余内容保留各次历史验收时的事实。

> 本文主体保留首次离线验收时的状态。随后已完成一次 DeepSeek 真实调用，进展见文末“配置加载与首次真实调用”；文中的“真实请求为 0”指首次离线验收，不代表最新状态。

> 最新[蓝图落实审计](blueprint-audit-2026-09-08.md)确认第一阶段同时存在实现缺口和验收缺项；本记录中的“联调停止点”不代表所有测试前功能已经完成。

日期：2026-09-08。此记录补充早期 `m0-m1.md`，按施工方案 M2—M4 整理新增实现与离线证据。当前交付可运行人工工作台、独立 Fake worker、固定精确计算、快照导出和恢复流程。**真实 DeepSeek / GLM 请求为 0；M3 的真实接口回归与 M4 的完整放行尚未通过。**

## 阶段与证据

| 范围 | 已实现及离线证据 | 尚需验证或限制 |
| --- | --- | --- |
| M2 人工工作台 | 图与工作稿共享快照，节点详情、版本比较、批注、证明方案、采用与审查、分支选择、冲突选择、运行控制、布局保存；`test_workspace_api.py` 覆盖布局 CAS、段落并发/ABA、批注锚点、冲突父版本、跨分支搜索与重开 | 图形端操作证据见浏览器测试与本轮截图；不能用 API 测试替代所有真实用户可用性评价 |
| M3 受控运行 | 独立 HTTP worker、并发槽、续租、检查点、请求预算预留与结算、未知结果阻断、人工作对账；`test_runtime_ready.py`、`test_fake_runtime.py` 及 `test_remote.py` 覆盖故障和适配器协议 | 真实服务的流式格式、usage、鉴权、模型名、超时、取消后计费、限流行为尚未经账户验证 |
| M3 审查 | 审查任务绑定指定版本及依赖，旧版本审查不迁移；取消/迟到审查不污染当前目标；人为录入不能冒充模型或形式检查 | 离线提供方响应和预写内容只能验证协议及证据落库；未完成真实独立模型审查质量评价 |
| M4 计算与研究样例 | `tools/hermitian.py` 实际执行 `Fraction` 精确矩阵运算；main 保留原猜想、失败路线、反例，`analytic-labels` 新建解析标签问题；fixture 与 `test_exports_recovery.py` 保留范围 | 研究文字为预写示例，精确证据为 `partial`，完整文字推导待人工审查；本工具不实现一般谱理论或任意代码执行 |
| M4 导出 | `POST /exports` 在同一事务固定分支事件快照；认证下载 ZIP 的 Markdown 和 manifest 来自不可变命令回执；包含原题、版本父链、旧依赖、上下文、条件、缺口、证据范围、采用理由、批注、冲突、运行和请求记录 | 当前是研究工作稿；不替代文章级符号、引用与整体论证检查。所需计算产物以 JSON 元数据保存在 manifest/正文中 |
| M4 备份恢复 | SQLite 在线 backup 可读取运行中的 WAL 数据；数据库副本移除凭据并 vacuum，ZIP 提供大小/SHA-256；恢复仅写新空目录，校验路径、哈希、完整性和外键 | 新环境须重新提供用户/worker 凭据；不会自动启动 worker，也不会把中断变成研究成功 |

## A01—A12 的离线证据映射

| 验收项 | 现有证据及待办 |
| --- | --- |
| A01 对象、稿件与重开 | `test_state_api.py`、`test_workspace_api.py` 验证原题、版本、回执与稿件持久化；浏览器图稿操作另见本轮端到端证据 |
| A02 修改运行输入 | `test_fake_runtime.py` 验证旧输入保留、影响回执、迟到候选隔离和恢复读取新版本；真实模型复跑待联调 |
| A03 替代证明 | `test_state_api.py`、`test_support.py` 验证 AND 共同前提、OR 替代支持及撤回不代表命题为假 |
| A04 条件与循环 | `test_support.py`、`test_state_api.py` 验证未证假设保持条件状态、无依据循环不自举为支持 |
| A05 并发与候选 | `test_state_api.py`、`test_workspace_api.py` 验证多个写入者、CAS 冲突、明确选择及版本父链；真实 worker 交互待联调 |
| A06 图稿与 SSE | `test_http_transport.py` 验证真实回环 HTTP/SSE 及补发；`test_workspace_api.py` 验证布局和稿件版本；浏览器同步另见端到端记录 |
| A07 暂停、插话与停止 | `test_fake_runtime.py`、`test_runtime_ready.py` 验证请求/生效边界、并发继续与迟到隔离；外部不可取消调用用离线故障注入验证 |
| A08 崩溃、重试与不明结果 | runtime 测试验证租约恢复、预留回执重放、未知/已耗用结果阻断；恢复测试验证旧令牌失效且不会虚构成功 |
| A09 证据类型与失败范围 | 支持与状态测试拒绝数值/局部证据升级一般证明；Hermitian 失败活动区分数学路线失败与 API/运行故障 |
| A10 历史审查与异议 | state/workspace/runtime 测试绑定目标、上下文和依赖；导出测试保存旧定义、精确计算产物与局部异议 |
| A11 预算竞争 | `test_runtime_ready.py` 验证并发原子预留、释放、消费和未知请求占用；额度单位为请求次数，不是货币上限 |
| A12 样例、导出、恢复 | `test_exports_recovery.py` 验证实际精确运算、原猜想及派生问题、导出重开字节一致、活动任务中断和未知请求阻断；真实研究/审查演示尚待 API |

这些条目说明已经存在的离线证据，并不把 A01—A12 整体标为完成。M4 的正式出口还要求真实提供方回归、真实研究结果记录及用户操作证据。

## 导出与恢复操作

网页导出当前分支，或通过 `POST /exports` 提交 `{project_id, branch_id, object_ids?}`；指定对象时连同原问题、有关反驳目标、上下文、证明与证据依赖一起导出。返回 `export_id`、`download_url` 和 `snapshot_seq`，下载需有效用户会话。相同命令键返回同一快照，后续编辑不会改变已生成的 ZIP。

在项目根目录的 PowerShell 执行：

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts\backup.py --database data\mathagent.db --output backups\research-2026-09-08.zip
.\.venv\Scripts\python.exe -X utf8 scripts\restore.py --backup backups\research-2026-09-08.zip --destination data\restored-research
```

输出 ZIP 不得已存在；恢复目标必须为新目录或空目录。备份只打包数据库和 manifest，不打包 token 文件。嵌套凭据字段与已知凭据字节从副本中移除，vacuum 清除旧页残留；源库不改动。恢复后 `restore-report.json` 列出状态变更：排队/运行任务变为 `interrupted`，已请求暂停/取消分别落为 `paused`/`cancelled`；已发出但结果不明的请求变为 `unknown`，相关运行 `reconciliation_required`。旧执行令牌失效，真实 API 项目许可关闭。先查看报告，再明确恢复所需任务。

## 验证记录

本子项实际执行：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\integration\test_exports_recovery.py
.\.venv\Scripts\python.exe -m ruff check services\api\src\mathagent\exports\service.py services\api\src\mathagent\tools\hermitian.py services\api\src\mathagent\api\artifact_routes.py scripts\backup.py scripts\restore.py tests\integration\test_exports_recovery.py
```

导出/恢复集成 **8 项通过**，包含篡改哈希、路径穿越和重复条目拒绝，以及未知请求对账后仍保持原取消意图；测试使用新临时数据库、实际 HTTP TestClient、运行中 SQLite 在线备份及独立 CLI 子进程。两个 Starlette/AnyIO 上游弃用提示不影响测试。项目整体测试、前端构建、浏览器结果与截图由本轮总验收补充，不由本子项推断。

## 真实 API 联调入口与后续范围

真实调用仍需要本机总开关、实际密钥与模型名称、项目允许的提供方、对应 worker 及剩余额度同时满足；密钥只在本机环境变量中填写。按 README 的联调入口先使用小请求数的新项目，分别记录研究输出和独立审查。保留实际请求 ID、输入版本、usage、完成/未知状态与数学结果。若模型没有发现反例，如实记录；不得改用预写答案冒充自主成果。

M5 的完整分支三方语义合并、条件解除、选择性重查、旁支转项目、永久删除与资产复用尚未实现。已有版本搜索和局部导出属于基础能力，不代表 A13—A18 通过。M6 的隔离代码执行、局部形式化、跨项目权限/复用、更多模型与文章级整理仍为后续范围，A19—A22 未验收。300 对象/1,000 关系的性能目标也需独立测量。

## 本轮总验收（MathAgent 0.2.0）

2026-09-08 在当前工作树完成以下验证，真实模型请求为 0：

- Python 全量 **157 项通过**（49.41 秒），包含状态、迁移、真实 HTTP/SSE、工作台、独立 worker 进程、提供方模拟传输、预算及恢复回归；报告为 [python-results.xml](python-results.xml)。另有两个上游 Starlette/AnyIO 弃用提示，未隐藏。
- Chrome 浏览器 **8 项通过**（44.0 秒）：手工研究与笔记版本、运行中修改引理及迟到隔离、双编辑者冲突、布局/SSE 与手机宽度、可选工具契约、精确示例导出、请求账本及产物导航、审查窗口固定版本与历史批注。报告为 [browser-results.json](browser-results.json)。随后改动只涉及执行版本提示文案与截图滚动位置，生产构建通过，示例导出单项复跑通过。
- TypeScript/Vite 生产构建、全项目 Ruff、PowerShell 启动脚本语法、OpenAPI 生成、Python wheel/sdist 打包通过。构建有约 826 KB 主脚本分块提示，未作为错误忽略业务失败；当前本机加载正常。生成的 wheel 不内嵌前端，分离安装方法见 README。
- `scripts/dev.ps1 -SkipBuild` 已实际启动本机 API 与默认 fake worker；健康检查返回版本 0.2.0、`real_models_enabled=false`。实际浏览器通过同源 cookie 操作，未读取或显示本机密钥。
- 300 对象、1,000 关系、100 个草稿证明方案、400 条证明依赖的真实回环 HTTP 测量见 [performance.json](performance.json)。2 个并发写入客户端的 40 次命令回执 p95 **415.882 ms**；20 次完整快照 p95 **140.234 ms**。这是合成数据的本机一次测量，未测量“提交至界面更新 2 秒”的端到端性能指标，也不把两个命令客户端称为两个模型 worker。

界面证据：[运行中改引理与版本隔离](screenshots/lemma-version-isolation.png)、[研究地图](screenshots/research-map-desktop.png)、[手机工作稿](screenshots/manuscript-mobile.png)、[保留原问题的解析标签旁支](screenshots/hermitian-derived-research.png)。

可选 WebMCP 的注册、参数拒绝和持久化动作在注入注册器中通过契约检查；当前浏览器没有实际扩展验证上下文，因此不宣称真实扩展集成通过。普通网页功能不依赖这一扩展。

**停止点：离线实现和 API 接入准备已验证；下一步需要用户在本机配置一个真实提供方的密钥与模型名。** 尚未验证账户鉴权、真实生成质量、实际流式/usage 行为或计费。当前远程输出只在完整结构化结果就绪后展示；没有逐 token 界面、已接受请求的远程取消/查询、任意代码/形式工具。实际模型审查固定为明确范围的局部审查，不自动采用或建立整份证明支持。

适配器协议对照了官方文档：[DeepSeek JSON](https://api-docs.deepseek.com/guides/json_mode/)、[DeepSeek 对话接口](https://api-docs.deepseek.com/api/create-chat-completion/)、[GLM 对话接口](https://docs.bigmodel.cn/api-reference/模型-api/对话补全)、[GLM 结构化输出](https://docs.bigmodel.cn/cn/guide/capabilities/struct-output)。查阅公开文档与真实账户请求是不同的验证步骤。

## 配置加载与首次真实调用（2026-09-08）

API 与独立 worker 现在会在启动时读取工作目录下的 `.env`，已有进程环境变量优先。`dev.ps1` 统一两者的工作目录；worker 令牌默认位置也按加载后的数据库目录确定。解析器只接受明确的应用变量，支持 UTF-8/BOM 与带 BOM 的 UTF-16，格式错误不回显内容。自动测试、固定演示和性能测试通过 `MATHAGENT_LOAD_ENV=0` 跳过本机配置；用户 `.env` 保持 Git 忽略。

本次单独创建“DeepSeek 接入验证（单次调用）”项目，项目与任务额度均为 1。真实 worker 完成公开算术题 `2 + 2`，返回 `2 + 2 = 4。`，产物仍为 draft。账本记录 1 次 spent、0 次 unknown、余额 0；实际 usage 为输入 678、输出 83、合计 761 tokens。详细状态见 [脱敏联调记录](deepseek-smoke-2026-09-08.json)，其中不保存密钥或模型配置值。

这次验证覆盖配置读取、真实账户鉴权、正常研究响应、结构化产物落库和请求结算。GLM 尚未配置；独立审查、复杂研究质量、超时、取消、限流与异常计费场景未据此验收。此次正常调用不能替代 M3/M4 的完整放行。

新增 20 项配置解析测试和 3 项 API/worker 启动集成测试，均使用合成临时配置；Python 全量 180 项通过（43.00 秒，两个已知上游弃用提示），浏览器 8 项回归通过（38.0 秒）。最新 Python 报告见 [python-results.xml](python-results.xml)，浏览器报告见 [browser-results.json](browser-results.json)。Ruff 与 PowerShell 语法检查通过。
