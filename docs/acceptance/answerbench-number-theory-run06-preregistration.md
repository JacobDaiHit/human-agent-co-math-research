# 数论 run06 执行前登记

日期：2026-09-13。用户已授权环境修复及补齐功能后的数论复跑，重启后真实沙箱隔离验收 3 项及独立 API 配置传递检查 1 项通过。尚未发起本批模型请求时登记本页。

- 唯一题目：`imo-bench-number_theory-081`，使用既有固定 problem-only fixture；另外三题不重跑。
- 新输出目录：`data/benchmarks/answerbench-number-theory-run-06`，不续写旧批次。
- 历史总额度 12 次：run03 spent 1、run04 spent 2、run05 unknown 1，共占用 4；本批最多 8 次。run03 用户确认已受理；run05 仍未知，不作免费或完成处理。
- 模型明确为 `deepseek-v4-flash`，High、thinking enabled、`reviewed_answer`。长度恢复 high；未知结果继续占用，整棵任务族最多预算内自动续试一次。
- 每请求最多 65,536 输出 tokens、600 秒；单题 3,600 秒；max steps 6、children 3、depth 2、reviews 2、parallel cases 1。所有子任务/恢复共享根预算。
- 开启离线代码沙箱，固定镜像 `sha256:8ff4228d908d291e4963f023bf02413ac35e10d6fb742cc22f5728c0c82b1874`；Python 标准库，无网络、宿主挂载或模型密钥。镜像下载是执行前依赖准备。
- 不提供网页、参考文献、答案键、旧数学进度或人工解题策略。求解源码在请求开始前提交冻结，runner 保存源码、题面、指令及镜像指纹并检查未变；运行期间不改求解源码。
- 终态后由独立本地评分脚本读取答案键。短答案评分不代表完整证明正确。启用沙箱后的配置与旧无沙箱样本不同，结果单列，不混算同配置准确率。

命令：

```powershell
.venv\Scripts\python.exe scripts/imo_answerbench.py --execute --output data/benchmarks/answerbench-number-theory-run-06 --case-id imo-bench-number_theory-081 --model deepseek-v4-flash --effort high --length-recovery high --unknown-recovery once --request-budget 8 --max-steps 6 --max-output-tokens 65536 --request-timeout 600 --case-timeout 3600 --parallel-cases 1 --code-sandbox
```

本地 `.env` 只补充沙箱镜像配置，其他配置不输出、不改写。执行时也显式传入已验证镜像 ID。
