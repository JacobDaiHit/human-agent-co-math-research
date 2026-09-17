# 数论定向复测 run09

用户授权重跑数论原题；仅运行 `imo-bench-number_theory-081`，使用通用空解集指导 `research-operations-v9`，没有向求解器提供本题答案、历史推导或人工提示。

## 冻结配置

延续 run08 的 DeepSeek `deepseek-v4-flash`、High、闭卷、离线固定 Docker 沙箱、精确计算器、`bounded_search_v1` 和 `reviewed_answer`。本轮使用上一授权组剩余的 16 次请求、262,143 输出 token，单次上限 65,536；检查池预留 4 次/65,536，收尾池 1 次/32,768，探索池 11 次/163,839。三路线、两活跃路线、每路线六次推进、最多两次修复。单请求 600 秒、全题 3,600 秒。未知占用保留、最多预算内续试一次；长度恢复 high。

这是更换提示及预算分配的开发复测，不是同预算能力对照。配置位于 `data/evaluation/number-theory-run09-search-config.json`。

## 结果

北京时间 2026-09-13 18:36:51 至 18:40:09，耗时 198.54 秒。两次已结算请求，没有未知请求。输入 13,770、输出 52,573、合计 66,343 tokens。首次输出 38,682，格式修复输出 13,891，均正常 stop，没有长度截断。

两次响应都提出了排除解的路线，但研究模式输出了非空 `structured_gaps`。离线重放原始响应，均触发 `Only reviews can report structured_gaps` 校验错误。系统拒收整个结果，规划工作失败，没有保存路线、候选或独立审查，随后因 `no_deliverable_candidate` 终止为 `step_limit`。这不是请求或探索池额度耗尽。

这次可观察到模型考虑了无解方向，但没有完成证明；不能据此证明提示修改带来了能力提升。根本阻断点是角色字段约束及格式恢复未成功，不能把无最终答案解释为数学答案错误。

原始目录 `data/benchmarks/answerbench-number-theory-run-09/`，报告和 `research-export.zip` 均保存。本次导出成功不表示此前含假设文本的导出缺陷已修复。批次确认 `source_unchanged=true`、`all_unattended=true`、人工干预为零、求解期间 `answer_key_loaded=false`。求解器退出后才执行独立本地评分：`missing_final_answer=1`、`mathematically_incorrect=0`；不是官方评分。

本授权组累计 run08/run09 已用 10 次请求、314,718 输出 token，剩余 14 次请求、209,570 输出 token。所有失败用量保留，不自动追加新付费运行。
