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

人工只在整批完成后检查结果、定位设计问题。修复后对相同四题新建批次，原批次不覆盖；修复必须是通用机制，不把正确答案或针对某题的方法写入提示或工具。

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
