# IMO-AnswerBench 四题无人试测

日期：2026-09-08。目的：先完整执行四个固定实例，从无人工解题干预的实际运行发现并修正系统设计问题。用户期待最终提高到 $100\%$；这是优化目标，不能预先承诺，也不以重复抽题或只保留成功运行达成。

## 固定材料与评分边界

采用 Google DeepMind `superhuman` 仓库 commit `80b2527a0b4e4bfc6a8b28825fadbdcfdd6048a1` 的 `imobench/answerbench_v2.csv`。数据与许可、来源哈希、上游 CSV 异常处理及抽样过程见[fixture README](../../fixtures/imo_answerbench/README.md)。

四领域分别对题目 ID 计算 `SHA256(mathagent-imo-answerbench-four-v1|ID)`，取摘要最小的一题；抽样不依赖题面、答案或运行成绩。固定题目为 algebra-004、combinatorics-037、geometry-055、number_theory-081。失败后不更换实例。

IMO-AnswerBench 检查短答案，官方方法是 Gemini AnswerAutoGrader 根据数学语义核对完整输出中的答案。本项目先使用[独立离线评分器](../../scripts/score_answerbench.py)：只对明确最终答案进行保守等价检查，未知表示 `ungraded`。它不等于官方判卷，不打证明分；可另审查论证质量，但不得混作 AnswerBench 答案分。

## 首批预设

| 项目 | 固定设置 |
| --- | --- |
| 模型 | 请求官方接口 `deepseek-v4-flash`；不使用 `.env` 里的视觉实验型号冒充榜单模型 |
| 思考 | `thinking.type=enabled`，`reasoning_effort=max` |
| 额度 | 每题 12 次请求，四题合计最多 48 次；格式修复及全部后代共用 |
| 探索 | 最多 6 个主任务步骤、3 个后代、深度 2、最多两轮审查 |
| 时间与输出 | 单请求 600 秒和 65,536 输出 tokens 上限；单题 1,800 秒；两题并行 |
| 自主性 | 只给题面及统一任务指令；数学方法由 Agent 决定；保存候选、请求独立审查并自行收尾 |
| 失败 | 保留错误、截断、预算耗尽和结果不明；其他题继续；不手工提供解法或中途追加额度 |
| 隔离 | 每题独立数据库与 API 进程；只暴露受控项目操作和固定精确计算 |
| 网络 | 模型没有检索工具；实际外部 HTTP 仅允许官方推理 endpoint；拒绝重定向及原生外部 tools |
| 答案 | 解题进程只读 `problem-only.json`，不加载答案键、评分结果、原始 CSV 或题解缓存 |
| 固化 | 每批保存源文件及依赖锁哈希、题面哈希、参数、每个请求 payload/usage、原文和导出 |

人工在运行结束后检查结果、定位设计问题。按用户后续要求，修复后只对失败或受影响的固定题目新建定向批次，原批次不覆盖；修复必须是通用机制，不把正确答案或针对某题的方法写入提示或工具。

## 执行与恢复

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts\imo_answerbench.py --execute --output data\benchmarks\answerbench-four-run-01
.\.venv\Scripts\python.exe -X utf8 scripts\score_answerbench.py --batch-dir data\benchmarks\answerbench-four-run-01
```

`--resume` 只允许同一配置、题面与源码。已完成题不再派发；中断后的未知请求仍占额度，不自动重发。每个题目的 `checkpoint.json` 保留创建回执和绝对截止时间，OS 文件锁阻止同时运行同一批次。整体报告区分运行完成、最终答案存在、数学评分和未知请求。

## 比较与解释

DeepSeek 发布的 V4-Flash IMOAnswerBench Pass@1 为 High $85.1\%$、Max $88.4\%$，见[官方模型卡](https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash/blob/main/README.md)。官方 API 当前文档把 `deepseek-v4-flash` 映射至 `DeepSeek-V4-Flash-0731`，见[模型说明](https://api-docs.deepseek.com/quick_start/pricing/)。记录中的模型字段是请求名称，不冒称已经从响应核实精确权重版本。

本批是四题、可用多次请求的 Agent 流程；单题多次推理及审查成本不能忽略，也不能直接与整个题集的单次模型 Pass@1 数字比较。当前 65,536 tokens / 600 秒为本地试测设置，并无证据与官方数学评测预算完全一致。公开题即使禁用检索也可能存在预训练记忆，这项限制单独记录。

## 项目管理

文献检索先列入蓝图的可选后续能力，默认关闭，本批禁用。研究数据、历史失败与正式报告保留；可重建的浏览器临时输出在正式截图/状态哈希核对后清理。当前目录删除受到自动审批策略阻断，未声称清理成功；不会为清理绕过审批或删除运行中的预览数据库。

## 首批真实结果：answerbench-four-run-01

首批于 2026-09-08 10:49:36 至 11:08:28 UTC 执行，采用上述预设；四题均无人工干预。基线提交为 `11aadf3`，启动时另以工作树逐文件字节哈希固化 45 个源文件和依赖配置，整体指纹为 `e03a8c53bbf33a90c895a3e356ee16f82b0fd3b71329944ed8721bb566405c8b`。结束报告记录 `source_unchanged=true`。部分文件存在混合换行，工作树字节哈希不应直接当作 Git blob 哈希；后续修复与后续批次另行记录。

原始证据保存在本地 [批次报告](../../data/benchmarks/answerbench-four-run-01/report.json)、[独立评分](../../data/benchmarks/answerbench-four-run-01/scores.json) 及各题子目录中的 `report.json`、`dispatch-*.json`、SQLite 数据库和导出文件。解题结束后才运行评分器，原始失败不覆盖。

| 实例 | 真实请求 | 主任务步骤 | 独立 LLM 审查 | 输入 tokens | 输出 tokens | 剩余请求额度 | 本地答案检查 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| algebra-004 | 4 | 3 | 1 | 27,310 | 37,515 | 8 | 明确最终答案等价通过 |
| combinatorics-037 | 4 | 3 | 1 | 30,078 | 142,643 | 8 | 明确最终答案等价通过 |
| geometry-055 | 4 | 3 | 1 | 26,927 | 46,728 | 8 | 明确最终答案等价通过 |
| number_theory-081 | 1 | 1 | 0 | 5,775 | 58,323 | 11 | 缺少合规最终答案 |
| 合计 | 13 | 10 | 3 | 90,090 | 285,209 | 35 | 3 题通过，1 题未交付明确最终答案 |

请求数包含审查调用；审查使用独立运行和请求，不额外计入主任务步骤。Token 数取提供方返回的 usage，总计 375,299；其中输入缓存命中 30,848 tokens。13 次请求均为 `spent`、`complete=true`、`finish_reason=stop`，可见原输出均未截断，未出现超时、结果不明或调用失败。实际操作只有 4 次 `write_draft` 和 3 次 `request_review`，全部执行成功，没有被拒绝的操作，也没有调用精确计算工具。

数论题暴露了通用收尾缺口：首个原始响应缺少 `next_action`，当时协议将其默认为 `finish`。该响应的正文仍明确表示待审草稿，动作仅保存候选，没有请求审查，却被运行状态机标为 `completed`；额度仍剩 11 次。评测报告据此保留 `state=completed` 与 `completed=false` 的区别。评分文件中的 `incorrect / no_clear_final_answer` 表示本地规则未收到可明确提取的最终答案，**不表示已经证明候选数学结论错误**，也不推断官方 AnswerAutoGrader 会如何评分。几何题最终响应同样省略该字段，但此前已有审查并交付了可提取答案，进一步说明缺字段本身应由协议显式处理。

三次审查均为同提供方、同模型的 `llm_review`，记录 `verdict=passed`、`coverage=partial`。审查的固定目标版本仍保留为对应对象的分支当前版本，且没有被后续正文覆盖；最终收尾分别保存为其他对象的新版本，正文与已审版本不同，也没有直接针对最终版本的审查记录。审查结果不能自动扩大为最终全文已验证。原输出中虽有“人工数学复核”等措辞，实际没有人工判卷或形式化验证；数论候选所依赖的外部定理也尚未获得独立核验。本次审计不凭记忆判断该定理真伪。

13 份实际 dispatch 的 payload 哈希与 13 份可见 raw 输出哈希均核对一致。逐层解码消息后，每份仅出现本题题干，未发现其他三题的题干、项目/对象/版本 ID 或答案键字段；求解计划和结束报告均记录 `answer_key_loaded=false`。四题均为 `model_web_tools=false`，全部记录的外部请求只指向 `https://api.deepseek.com/chat/completions`，payload 无 `tools`、`functions` 或网页搜索配置，传输层另限制该推理出口并禁止重定向。本地 API 通信不属于外部检索。以上结论针对应用及其留存请求，不证明模型预训练没有接触过公开题目。

因此，首批结果为四题中三题的明确最终答案通过本地保守等价检查（$75\%$），另有一题未完成合规收尾。它既不是四题证明质量全部通过，也不是官方全量 IMO-AnswerBench 成绩。后续只修复通用协议、收尾与审查机制，不将本批答案或特定解法注入再次求解。

## 首批后修复与同题复测

协议升级为 `research-operations-v4`：自主研究必须明确给出 `next_action`，缺失时进入有额度约束的结构修复，首次无效响应的操作不执行。非自主草稿及历史输出仍保留兼容处理。

评测启用可选完成策略 `reviewed_answer`，普通研究默认仍为 `draft`。候选先保存完整论证及一个方框答案；独立审查必须针对该候选的当前版本，且原题、上下文等审查输入未变化。父任务须实际收到完整审查正文（或按版本续读完整分页），最终引用该候选并沿用其中的方框答案。缺少这些条件会保存明确回执并继续，直到原步骤、请求或时间上限；不会补写答案来制造成功。旧完成接口不能绕过该检查。

精确被审查的产物是候选版本，最终正文只是引用它的摘要。机械完成门槛不判断数学真伪，也不把局部 LLM 审查升级为形式化证明。报告另列 `runtime_completed`、`final_answer_present`、`review_completed`、`finalized_after_review` 和 `workflow_completed`，以步骤回执保留完成检查的证据。

提供给模型的额度快照同时考虑项目、分支、任务及后代共用限制，保留 `unknown` 占用。格式修复会扣除已知消耗，并明确标注该快照可能已过时；最终是否可派发仍由账本控制。评分器修订 2 另存 [scores.v2.json](../../data/benchmarks/answerbench-four-run-01/scores.v2.json)，区分未交付答案与数学不等价，未放宽答案提取或修改答案键。

第二批使用相同四题、Max、每题 12 次请求、6 个主步骤、两轮审查、单请求 600 秒及单题 1,800 秒。重新从题面独立开始，不继承首批草稿、评分或人为解题提示；首批数据保留。

### 用户调整：仅继续数论定向复测

第二批执行期间，用户指出只需复测首批受影响的数论题，不必重新付费运行全部四题。2026-09-08 11:57:42 UTC 通过正常事务命令暂停组合与几何题的后续推进，已派发请求在执行边界保存结果与费用；操作证据见 [用户停止冗余复测记录](../../data/benchmarks/answerbench-four-run-02/user-stop-redundant-retests.json)。代数当时已完成；组合在暂停前已经达到步骤上限；几何最终暂停。此后三题不再新增请求，只保留数论题独立继续。

三题额外回归共消耗 18 次请求（代数 5、组合 9、几何 4），应单列为额外回归成本，不能算作数论题所需成本。该批次存在用户调度干预，不能将整个批次宣称为无人基准成绩；原执行器硬编码的 `human_interventions=0` 也不能覆盖这份实际控制记录。数论题的题面、指令、独立数据库、额度及求解过程没有被修改。

组合题已有日志另显示完成检查过严：候选正文重复两次相同方框答案，虽已有通过的独立审查、完整审查阅读和正确引用，仍被拦截，连续收尾耗尽步骤。这个问题已用离线测试复现；修复及验证使用已保存的记录，不再次付费重跑组合题。后续遵循“代码全量回归，真实模型定向复测失败或受影响案例”，不默认把每次通用代码改动扩大为全题付费重跑。

### 第三次仅数论：截断恢复与定向运行

第二批数论最终失败于第二次响应：`finish_reason=length`，65,536 输出 tokens 已消耗，可见正文为空；账本仅消耗两次请求，尚余十次。未获得独立审查或正式最终答案。该案例没有人工干预，不能把流程失败写成数学答案错误。

通用修复将重复的同一候选方框答案视为一个明确答案，仍拒绝相互冲突的答案，最终摘要仍须只有一个方框。用第二批组合题的原始数据库只读重放最终完成检查，返回无缺口；不改写其历史失败状态。派生审计见 `data/benchmarks/answerbench-four-run-02/intervention-audit.v1.json`，真实人工控制次数为代数零、组合一、几何二、数论零（按受控任务事件计数）。后续执行器从持久化事件派生这些字段，定时取消通过精确回执单独标记为调度器操作。

新增 `length_recovery=high` 为显式可选设置，普通任务默认关闭。只对 DeepSeek 已确定消耗且明确达到输出上限的响应，在同一执行尝试内允许一次 High 恢复；未知结果和超时不重试。截断正文不执行任何操作。恢复和格式修复仍受原请求账本、输出、时间限制，单次执行尝试最多三次派发；所有实际思考强度逐请求留档。这是 Max 正常求解、High 截断恢复的自适应运行，不能称为纯 Max 成绩。提示协议版本为 `research-operations-v5`，只增加有限子目标与诚实保留缺口的通用指示。

第三次仅选择原数论题，不更换实例，不使用旧草稿或评分；上限仍为十二次请求、六个主步骤、单次 65,536 输出 tokens / 600 秒、单题 1,800 秒。只运行下列定向命令：

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts\imo_answerbench.py --execute --case-id imo-bench-number_theory-081 --length-recovery high --parallel-cases 1 --output data\benchmarks\answerbench-number-theory-run-03
.\.venv\Scripts\python.exe -X utf8 scripts\score_answerbench.py --case-id imo-bench-number_theory-081 --batch-dir data\benchmarks\answerbench-number-theory-run-03
```

定向计划冻结完整原题文件哈希及所选题号，求解目录只复制所选题面，最高总调用数相应缩为十二次。评分也显式指定相同题号，单题复测成绩不能与历史三题拼接成同一批无人运行或全量基准成绩。

启动前本地验证：全量 Python 测试 472 项通过（两项上游弃用警告），[XML 证据](../acceptance/python-answerbench-targeted-2026-09-08.xml)；执行器七项测试另对最终报告字段再次通过。Ruff、API 契约导出和前端构建通过；Vite 保留已有的大包体积提醒。本地测试只使用模拟推理，不计入真实模型调用。

### 第三次真实结果：未完成，保留未知请求

`answerbench-number-theory-run-03` 于 2026-09-08 12:35:47 至 12:44:38 UTC 执行，仅有原数论题；代码提交为 `2dace92`，工作树源码指纹为 `7dc3e1016a0d81e6288416db71d8c698084e01bf59cfeabd6bd2fb4d3f011eff`。结束时 `source_unchanged=true`、`all_unattended=true`、`human_interventions=0`；代数、组合、几何没有新增调用。

该题只派发一次 Max 请求，传输未正常结束，保存原因为 `transport_outcome_unknown`；可见正文为空，没有 `finish_reason` 或 usage，不能把缺失 usage 计为零费用。账本保留一笔 `unknown` 占用，剩余十一笔额度，运行状态为 `reconciliation_required`，没有自动重发或清除未知状态。它发生在单次十分钟总上限之前，不能据此断言是总时限超时或输出截断。High 恢复只对明确 `length` 响应生效，本次没有触发，真实服务上的恢复效果仍未验证。

见本地 [执行报告](../../data/benchmarks/answerbench-number-theory-run-03/report.json)、[该题完整记录](../../data/benchmarks/answerbench-number-theory-run-03/imo-bench-number_theory-081/report.json) 和 [离线评分](../../data/benchmarks/answerbench-number-theory-run-03/scores.json)。评分仅针对所选一题：没有最终答案，未完成；数学不等价答案数为零，不能把此运行故障解释为模型已经给出错误数学答案。没有证明、审查或成功结果可用于补足首批成绩。

当前可报告的是：首批三道题的明确最终答案通过本地等价检查，数论尚未完整跑通。通用流程缺陷已修复并通过本地回归，数论的真实复测又暴露了外部传输可靠性限制；没有得到同批四题全部成功或全量基准 $100\%$ 的证据。原失败、冗余回归成本及未知请求均保留，未再启动其他付费批次。
