# 数论定向复测 run10

用户授权先修复 run09 暴露的协议问题，再闭卷重跑原数论题。

## 本轮修复

提示版本 `research-operations-v10`。研究角色输出 schema 明确限制 `structured_gaps` 为省略或空列表，与服务端校验对齐；未决研究问题应保留在正文、findings 或公开的 report_gap 操作中。独立审查仍可填写结构化缺口，研究角色不能自行出具审查结论。

格式恢复现在携带有界的具体校验反馈（字段位置、类型、说明），不再仅笼统要求修正 JSON。不自动丢弃模型缺口、不执行被拒响应的操作，也不通过切换角色绕过校验。重试继续按现有预算结算。

对 run09 两份原始响应进行了离线重放：原始响应仍被拒绝并产生准确反馈；仅在内存副本中将缺口详情保留到 findings 并去除专用字段后通过校验。原始记录未修改，此操作不构成模型求解成功。

## 冻结计划

仅 `imo-bench-number_theory-081`。继续使用原授权剩余 14 次请求、209,570 输出 token，单次最大 65,536。沿用 run09 检查池 4 次/65,536、收尾池 1 次/32,768，探索池为 9 次/111,266。配置 `data/evaluation/number-theory-run10-search-config.json`。DeepSeek v4-flash、High、bounded_search_v1、reviewed_answer；固定离线 Docker 沙箱与计算器，无联网工具，无历史解题提示，无人工干预。单请求 600 秒、全题 3,600 秒；未知请求保留占用，最多预算内续试一次，长度恢复 high。

这是配置开发试验，不是同预算效果对照。求解结束后才独立执行本地评分。预算池回流及此前含假设文本的导出问题没有在本轮修复。

## 验证和实际结果

新增 `tests/providers/test_role_gap_repair.py` 两项测试通过：角色 schema/安全反馈，以及 MockTransport 经 RemoteProvider、HTTPWorker 完成失败结算与合法修复的流程。既有 providers、空解集和证据控制器、provider_pipeline、review_recovery、real_response_replay、agent_context、context_memory、bounded_search 相关测试均通过；修改涉及的 Ruff 检查通过。测试无真实模型请求。

北京时间 2026-09-13 18:50:41 至 18:57:57，耗时 435.62 秒。真实复测共 6 次 spent、零 unknown；输入 49,620、输出 111,265、合计 160,885 tokens。源码冻结检查通过，人工干预为零，求解期间未读答案键。导出成功。

| 调用 | 实际输出上限 | 实际输出 | 结果 |
| --- | ---: | ---: | --- |
| 规划 | 55,633 | 55,633 | length，无可见正文 |
| 规划长度恢复 | 55,633 | 33,799 | stop，合法保存三条路线 |
| 路线推进 | 10,917 | 10,917 | length，无可见正文 |
| 路线推进 | 5,458 | 5,458 | length，无可见正文 |
| 路线推进 | 2,729 | 2,729 | length，无可见正文 |
| 长度恢复 | 2,729 | 2,729 | length，无可见正文 |

本轮没有 `invalid_structured_output`，研究/审查字段错误未复现；但真实运行也未触发新的格式修复分支，该分支仅有离线模拟覆盖。三路线均派发过推进，没有候选或独立审查，最终因 `no_deliverable_candidate` 终止为 `step_limit`。本地独立评分为 `missing_final_answer=1`、`mathematically_incorrect=0`，不是官方评分。

探索池只余一个输出 token，检查及收尾仍预留 98,304。原授权组 run08 至 run10 累计已用 16 次请求、425,983 输出 token，剩余 8 次请求、98,305 输出 token。没有追加下一批付费请求。

本轮修复了 schema 与服务端角色校验不一致的问题，不能宣称已提升解题率。继续沿用固定分池使后续推理空间不足；应先修复预算分配与恢复策略，再开展有意义的能力复测。
