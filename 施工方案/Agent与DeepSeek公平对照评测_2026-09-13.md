# Agent 与 DeepSeek 公平对照评测

本方案用于后续预登记，不授权或启动付费运行。此前四题已经用于调试、失败分析和提示改进，全部属于开发集；run06 不重评分、不改成绩。公开题是否进入模型训练无法由本项目排除。

## 要测的提升

主指标是同题成对的最终答案准确率差：

\[
\Delta=\frac{1}{n}\sum_{i=1}^{n}\left(\mathbf{1}[\text{agent 第 }i\text{ 题正确}]-\mathbf{1}[\text{基线第 }i\text{ 题正确}]\right).
\]

同时报告请求数、实际输入/输出/总 token、缓存命中、耗时、缺答、评分器无法判断、传输未知和人工干预。内部审查结果单列，不作为两边证明质量的独立裁判。

### 比较组

| 组 | 做法 | 能说明什么 |
|---|---|---|
| 单次 DeepSeek | `--solver direct --request-budget 1`，自然语言回答，无项目工具 | 最容易理解的产品参考；agent 花费更多时，不能据此归因架构优势 |
| 多轮 DeepSeek 自查 | `--solver self_refine`，固定轮数，自查此前回答，最终采用最后一轮 | 与 agent 共享请求及累计输出上限的主要计算量对照 |
| Agent 无计算工具 | `--solver agent --no-calculator`，不开沙箱 | 与自查基线比较研究组织、状态管理和内部协作的整体收益 |
| Agent 加工具 | 同上但开启内置计算或离线沙箱 | 单独衡量工具增益；不能把计算工具收益全部解释为组织方式收益 |

前三组均使用 `--evaluation-mode answer`，在解答要求、最终答案提取和评分上保持一致。研究模式 `--evaluation-mode research` 另做可信证明实验；不将其严格完成率与裸模型的短答案准确率直接相比。

`self_refine` 是固定的、无标准答案参与的简单自查基线，不宣称它是最优推理策略。应在开发集上给基线和 agent 相近的调参机会，冻结后再上保留集。不能从多个候选中根据标准答案挑最佳，也不能只报告有利的预算档位。

## 题目和重复

1. 固定官方 `answerbench_v2.csv` 的提交与哈希，程序化排除已使用的四题。不要先看成功率或模型解答再选题。题目加载器只接受题号、类别、题面；答案键单独留给评分进程。
2. 先从四类各随机选两道新题，共八道，做有成本上限的探索性配对试验。它只用于检查流程及费用，不足以证明普遍提升；如果据此调参，这八题也转入开发集。
3. 正式确认再使用新的保留集，例如四类各十道，共四十道；至少三次独立重复。公开数据的训练污染只能说明未知，不能称为“绝对未见题”。先预登记划分种子、题号、排除规则和总预算。
4. 两边使用相同题序，`--case-order-seed` 只控制本地顺序，不是 DeepSeek 采样种子。重复之间改变题序并交替先运行哪一组；尽量让对应组在邻近时段运行，记录模型返回版本/请求时间。当前 runner 不提供题内两组自动交错调度，也不能保证供应商模型逐位可复现。
5. 每个条件、每次重复都使用全新输出目录和独立项目数据库，禁止跨题/跨组共享研究记忆、旧答案或审查意见。

## 预算公平的边界

本轮实现的 `--cumulative-output-token-budget` 是**输出（含供应商计入 completion 的思考）额度上限**，覆盖根任务、子任务、审查与恢复；不是输入加输出的精确总 token 上限，更不是美元硬上限。未知 usage 保留派发前额度，已报告 usage 据实计数。报告中的输入 token 与缓存命中另外汇总。

主对照可先统一为每题最多四次请求、每次最多 8,192 输出 token、累计最多 32,768 输出 token，High、相同请求/单题时限，不联网。八题、两组、一次重复的上限是六十四次请求、524,288 输出 token，另有输入成本；这是预算建议，尚未执行或获得新付费额度。

不能只比较“最多四次请求”，因为不同请求长度不同；也不能把相同输出上限称为相同总计算量。结果应画出或列表报告准确率随**实际总 token / 实际费用**的变化，比较多个预登记额度档位。若要宣称“相同费用下提升”，还需要冻结当时模型价格及缓存计费规则，统一计入全部推理调用，并按相近实际费用比较；当前代码不自动购买额度或强制美元上限。

两种模式共用未知结果策略，建议正式比较先统一 `--unknown-recovery stop`；若选择 `once`，必须两边都预登记，旧未知占用不退回。基线只对符合条件的传输未知续试，非完整输出直接停止；agent 自身的格式/长度恢复属于其工作流成本，配置与次数必须报告。

## 评分规则

- 新报告固定选择“最后一个有效根任务步骤中明确 `next_action=finish` 的单一盒装答案”；基线固定选择最后一次完整响应中的单一盒装答案。模型无答案应弃答，不强迫猜测。未完成步骤中的候选只作诊断，不能转成正式提交。
- 短答案评分独立于证明和运行完成状态。因此“明确提交的答案正确，但证明仍有问题”会如实反映在两个指标中；不会自动采用该结论为定理。
- 两组必须使用同一答案键、同一评分器版本。当前 `score_answerbench.py` 是保守本地等价比较器，不是官方 Gemini AnswerAutoGrader；它只支持部分答案类型。扩展至完整题集前必须先验证统一评分覆盖，无法判断的题不能删除。
- 对不支持的答案类型，可在两组结果冻结后，混合匿名交给同一套盲评流程；或另行实现、预算并冻结官方 AnswerAutoGrader。评分器可以看标准答案，但求解器不能；不能把评分意见回流到本次求解。
- 缺答/超时/预算耗尽保留在总分母；`ungraded` 单独报告及敏感性范围。若人工干预，应单列实验，不静默排除失败样本。旧报告仍用旧完成门槛，禁止事后挑草稿改分。
- 证明能力另行盲评：两组都提交证明，去掉模型/组别标识，由同一外部评审及统一细则检查。不能把 agent 的自家 reviewer 通过率当作独立证明正确率；本轮未实现形式化证明工具。

## 可用命令模板

以下路径为未来准备的保留集，不是现有四题。先准备只含题面的 `data/evaluation/holdout/problem-only.json` 与独立答案键，再预登记总预算。没有 `--execute`，runner 不会发起请求。

```powershell
# 主要对照：多轮裸 DeepSeek 自查（需要另行授权后才加 --execute）
.venv\Scripts\python.exe scripts/imo_answerbench.py --problems data/evaluation/holdout/problem-only.json --output data/benchmarks/holdout-self-refine-r1 --solver self_refine --evaluation-mode answer --model deepseek-v4-flash --effort high --request-budget 4 --max-output-tokens 8192 --cumulative-output-token-budget 32768 --max-steps 8 --request-timeout 600 --case-timeout 3600 --parallel-cases 1 --unknown-recovery stop --length-recovery none --case-order-seed 101 --no-calculator

# Agent 无计算工具；其余条件相同（同样未加 --execute）
.venv\Scripts\python.exe scripts/imo_answerbench.py --problems data/evaluation/holdout/problem-only.json --output data/benchmarks/holdout-agent-r1 --solver agent --evaluation-mode answer --model deepseek-v4-flash --effort high --request-budget 4 --max-output-tokens 8192 --cumulative-output-token-budget 32768 --max-steps 8 --request-timeout 600 --case-timeout 3600 --parallel-cases 1 --unknown-recovery stop --length-recovery none --case-order-seed 101 --no-calculator

# 两组终态后，使用同一份已验证可评分的答案键离线评分
.venv\Scripts\python.exe scripts/score_answerbench.py --batch-dir data/benchmarks/holdout-self-refine-r1 --answer-key data/evaluation/holdout/answer-key.json
.venv\Scripts\python.exe scripts/score_answerbench.py --batch-dir data/benchmarks/holdout-agent-r1 --answer-key data/evaluation/holdout/answer-key.json

.venv\Scripts\python.exe scripts/compare_answerbench.py --baseline data/benchmarks/holdout-self-refine-r1 --agent data/benchmarks/holdout-agent-r1 --output data/benchmarks/holdout-comparison-r1.json
```

多次重复使用新的目录，并重复传入匹配的 `--baseline` / `--agent`。同一次配对两组题序种子相同；不同重复可改变题序种子，其余条件、题目集合、源码、评分器和答案键必须保持冻结。比较器输出成对胜/负、准确率差、按题目聚类 bootstrap 的区间、单次重复的精确 McNemar 检验及实际 token 汇总；小样本区间宽或退化时不能只报点估计。

## 参考

- [IMO-Bench 官方论文：AnswerAutoGrader 与 ProofBench 的区别、重复试验设置](https://aclanthology.org/2025.emnlp-main.1794.pdf)
- [官方题库及版本说明](https://github.com/google-deepmind/superhuman/tree/main/imobench)
- [DeepSeek Chat Completions 参数和 usage 字段](https://api-docs.deepseek.com/api/create-chat-completion/)
