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

真实批次结果将在执行后补充，不用离线合成测试代替真实数学成绩。
