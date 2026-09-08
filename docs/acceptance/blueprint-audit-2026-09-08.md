# 蓝图落实与测试准入审计

日期：2026-09-08。范围：`D:\github\mathagent` 当前工作树。依据为蓝图 v0.2、施工方案 v0.1、现有源码、测试源码、归档报告和本地服务只读状态。用户已明确本轮只审计、列清缺口；本轮没有发起 IMO 或其他模型调用，没有修改应用代码或用户配置。

## 1. 结论

**尚未全部实现，而且第一阶段 M0—M4 也不是“功能已全部做完，只剩接入 API 和数学题测试”。** 当前交付的是有版本与证据管理的本地工作台，加上可控制、可记账的单次模型生成／审查运行器。已有相当一部分基础功能和工程测试，但最小自主研究操作、生成—审查—修订闭环、完整调用记录、故障输出保全等仍缺实现。

此前“已到真实 API 联调前停止点”的表述只能代表现有实现能够进行接口联调，不能等同于施工方案第一阶段已经完整实现。需要纠正这个范围判断。

施工方案第 2 节和第 10 节的出口是 **A01—A12 全部通过，并完成一次有证据记录的真实研究演示**。一次 `2 + 2` 的正常调用只通过了接入冒烟测试；180 项已有 Python 测试通过也不能证明尚未实现的需求已经满足。不使用任意的整体完成百分比。

> 本文是补齐前的历史审计；后续实现及验收事实见[一期补齐与研究地图验收](completion-assessment-2026-09-08.md)。

## 2. 阶段状态

| 阶段 | 当前判断 | 已有内容与边界 |
| --- | --- | --- |
| M0 地基 | 基础实现、离线验证已有；发布基线未固化 | 项目环境、依赖锁、迁移、API、FakeProvider 可运行；当前 Git 尚无可解析的 HEAD 提交。 |
| M1 状态闭环 | 核心语义已实现并有离线证据 | 不可变版本、CAS、基本分支、AND/OR 证明支持、条件标记、采用与审查分离、旧结果隔离。 |
| M2 人工工作台 | 大部分实现，有浏览器证据；部分操作仍缺 | 图稿联动、批注、版本比较、冲突选择、运行面板、布局保存；折叠、完整范围控制及额度耗尽恢复交互未齐。 |
| M3 受控运行 | 部分实现；核心 Agent 行为仍缺 | 独立 HTTP worker、两个执行槽、租约、请求预算、暂停与插话、一次生成或审查已接通；自主操作和修订循环没有实现。 |
| M4 第一版交付 | 部分实现，未达到放行条件 | 固定精确计算、导出、备份恢复已有；失败与来源记录不完整，真实研究演示和完整关键故障验收缺失。 |
| M5 资产积累 | 预定后续阶段，只有少量基础能力提前存在 | 已有正文版本搜索、局部导出、冲突候选选择；不等于完成语义合并、条件解除或成果复用。 |
| M6 工具与长项目 | 预定后续阶段，尚未实现 | 隔离代码执行、形式化、跨项目检索与权限、文章级整理。它们不是本轮新增的一期前置要求。 |

## 3. 第一阶段必须补齐的实现

下表是源码核对得到的差距；“补齐”是后续工作清单，本轮没有进行这些实现。

| 编号 | 需求与现状 | 影响及完成标准 | 主要证据 |
| --- | --- | --- | --- |
| G01 自主研究操作与修订循环 | 方案 §6.1 要求读对象、检索项目、写草稿、提出证明、记录失败、请求审查、提出子任务和讨论，并限制自动修订轮数。当前输出只有一份 JSON 草稿／审查；一次成功请求保存后即退出。 | 两个并发槽不能代替可协作的研究 Agent。需增加经过验证的操作协议、预算共享和持久步骤；在最多 2 轮的修订样例中实际完成研究→请求审查→按意见修订，并可插话或停止。 | [worker.py](../../services/api/src/mathagent/runtime/worker.py) L102–184；[protocol.py](../../services/api/src/mathagent/providers/protocol.py) L9–15；[施工方案](../../施工方案/数学科研多Agent工作台_施工方案_v0.1.md) L185–193。 |
| G02 输入上下文与证据状态 | 方案要求局部目标、原目标关系、相关尝试和证据状态，并可主动扩展上下文。目前打包整个分支当前对象及历史证明依赖；没有 Agent 可调用的项目检索，也没有完整的采用／支持／审查状态字段进入模型上下文。 | 模型难以明确区分可靠前提、暂定材料和过去意见，长项目也容易超限。需构造带状态及来源的局部输入包，提供受控的项目内查询，保留关键版本引用。 | [service.py](../../services/api/src/mathagent/runtime/service.py) L519–633；[protocol.py](../../services/api/src/mathagent/providers/protocol.py) L61–81；[workspace_routes.py](../../services/api/src/mathagent/api/workspace_routes.py) L39、L104。 |
| G03 调用配置与提示版本 | 已保存提供方、输入版本、指令、模式、请求关联号、成功时的 usage 和产物；没有冻结实际模型 ID、参数和提示模板版本。worker 调用时才从环境取得配置。 | 修改环境或模板后，旧执行不能完整说明当时究竟调用了什么。需对每次 attempt 固化不含密钥的模型配置、模板版本／哈希和有效参数，显示并随导出保存。 | [runtime_models.py](../../services/api/src/mathagent/persistence/runtime_models.py) L16–33；[service.py](../../services/api/src/mathagent/runtime/service.py) L607；[remote.py](../../services/api/src/mathagent/providers/remote.py) L114–120；方案 L210。 |
| G04 协议错误、截断与中断输出保全 | JSON 校验失败和部分流中断主要留下错误码；没有保存可见的原始输出／部分结果，错误对象也不带回已收到的 usage、请求关联号。未实现受预算约束的一次结构修复。 | 长证明被截断时，可能既消耗请求又丢掉可检查材料。需限量保存可见响应、完整性标记、哈希和已取得的计量元数据；修复调用占预算，失败后仍能查看原材料。保存前崩溃也需明确可恢复的提交边界。 | [remote.py](../../services/api/src/mathagent/providers/remote.py) L143–155、L185–231；[worker.py](../../services/api/src/mathagent/runtime/worker.py) L130–167；方案 L203。 |
| G05 失败与文献来源记录 | 失败四分类 UI 已存在，也能保存 outcome 和 evidence_scope；目前目标、假设、失败位置、证据、重试条件主要写在自由正文里。API payload 为通用字典。来源同样可手工放入正文／payload，但没有完整来源契约。 | 应称“部分实现”，不能说完全没有失败或来源功能。需绑定精确目标版本、适用条件和证据，校验反驳／方法障碍的范围；来源至少有题名、作者、链接／标识、定位及访问日期，并能随版本和导出保留。 | [ActionDialog.tsx](../../apps/web/src/ActionDialog.tsx) L20、L42–47；[schemas.py](../../services/api/src/mathagent/api/schemas.py) L30–35；[state.py](../../services/api/src/mathagent/application/state.py) L131；[test_review_regressions.py](../../tests/integration/test_review_regressions.py) L79；方案 L109、L224。 |
| G06 分支范围、权限与资源控制 | 暂停／停止／插话接口作用于一个 Run。没有分支范围的原子停止派工入口；项目自由度策略只写入默认值，实际可改的是提供方许可和项目请求额度。没有子任务树可应用深度、数量或范围上限。 | 需能暂停选定分支及其后续工作，同时让其他分支继续。Agent 获得操作能力时，研究自由度、资源／操作权限、采用规则要分别检查；子任务与重试共享额度并有明确上限。 | [app.py](../../services/api/src/mathagent/api/app.py) L260；[service.py](../../services/api/src/mathagent/runtime/service.py) L850–896；[state.py](../../services/api/src/mathagent/application/state.py) L144–147；[runtime_routes.py](../../services/api/src/mathagent/api/runtime_routes.py) L13–16。 |
| G07 额度耗尽后的恢复入口 | 后端允许从 budget_exhausted 恢复，但前端恢复按钮只在 paused／interrupted／failed 出现。运行创建后也没有更新该运行请求上限的入口。 | 用户给项目增加额度后，原任务仍可能无法从界面续跑；若耗尽的是任务上限，单改项目额度也不足。需连通相应调整与恢复操作，并用耗尽→增额→原 Run 新 attempt 的场景验收。 | [RunPanel.tsx](../../apps/web/src/RunPanel.tsx) L10；[service.py](../../services/api/src/mathagent/runtime/service.py) L911；[RunOptions](../../services/api/src/mathagent/persistence/runtime_models.py) L16–20。 |
| G08 工具协议与可追溯产物 | 实际精确 Hermitian 计算存在，但通过固定的人类示例入口触发；没有通用的受控工具调用／结果契约供 Agent 使用。产物目前以正文和 JSON 元数据为主，未完整实现计划中的文件产物存储与中断一致性流程。 | 一期只需接通经过审查的固定计算，不要求任意代码沙箱。应记录工具、版本、输入、实际输出、状态和适用范围；若一期需要文件产物，再落实原子落盘、哈希和引用一致性。不能把“固定示例可生成”直接算作“Agent 能调用计算能力”。 | [artifact_routes.py](../../services/api/src/mathagent/api/artifact_routes.py) L19–48；[hermitian.py](../../services/api/src/mathagent/tools/hermitian.py)；[exports/service.py](../../services/api/src/mathagent/exports/service.py)；方案 L99、L126、L220–222。 |
| G09 人工工作台的小范围缺口 | 图已有筛选、拖动位置保存和普通缩放；没有研究路线折叠。隐藏节点／归档分支没有独立状态和操作。 | 需补齐一期图的折叠能力；隐藏、归档等操作需按计划明确交付阶段，不能与停止、撤回混称完成。语义缩放和复杂分支比较仍可按原计划放到后续。 | [ResearchGraph.tsx](../../apps/web/src/ResearchGraph.tsx) L14–36；[Branch 模型](../../services/api/src/mathagent/persistence/models.py) L33–41；方案 L234、L247。 |

资源计量的限定：当前按请求次数而非金额计费，并且界面已明确这一点。方案允许在没有可靠费用上界时先使用请求次数和输出上限，所以“未实现人民币／美元预算”本身不应被误列为一期阻断。分支额度、任务调额和未来子任务共享额度仍需按约定补齐。

## 4. A01—A12 逐项核对

“核心离线通过”表示对应规则有直接测试断言，不表示所有真实模型及完整用户流程已经通过。“部分”可能同时包含实现缺口和验收证据不足。

| 验收项 | 当前状态 | 已有证据 | 仍需完成 |
| --- | --- | --- | --- |
| A01 创建、持久化、重开 | 核心离线通过 | [test_state_api.py](../../tests/integration/test_state_api.py) L521：对象、稿件、版本和回执重开后保留；浏览器有人工工作流。 | 随最终版本保留可重复的完整交付记录。 |
| A02 运行中改引理 | 机制有 Fake 与浏览器验证；真实场景待验 | [test_fake_runtime.py](../../tests/integration/test_fake_runtime.py) L116：旧输入保留、迟到候选、恢复读新版本。 | 真实请求在途时修改引理，核对影响、旧结果和下一次输入。 |
| A03 共同前提与替代证明 | 核心离线通过 | [test_state_api.py](../../tests/integration/test_state_api.py) L305、L333：AND／OR、撤回一条仍保留另一条。 | 最终回归保持该规则；无须用模型自评代替领域测试。 |
| A04 未证假设与循环 | 核心离线通过 | [test_state_api.py](../../tests/integration/test_state_api.py) L410：条件标记、证明循环不自举、普通研究关系可有环。 | 自主 Agent 新增对象／证明后也必须经过同一校验。 |
| A05 两 worker 与人并发修改 | CAS／候选机制通过；完整场景未验 | [test_state_api.py](../../tests/integration/test_state_api.py) L220 的三个写入者实际均使用 human API。 | 真正的两个研究工作者与人共同修改，保留全部候选；G01 操作协议需先到位。 |
| A06 图稿、布局与 SSE | 核心离线及浏览器通过 | [test_http_transport.py](../../tests/integration/test_http_transport.py) L121：真实本机 HTTP／SSE；工作台和浏览器覆盖稿件与布局。 | 现有性能测量未覆盖“提交到 UI 更新”的完整 2 秒目标。 |
| A07 分支暂停、引导与停止 | 部分实现 | 单 Run 的请求／生效、在途请求和迟到隔离已有 Fake／模拟传输测试。 | 补分支范围控制；验证同分支后续工作被阻止、别的分支继续，不能用暂停一个 Run 代替。 |
| A08 杀 worker、重启、未知请求 | 恢复规则通过；完整故障演练缺证据 | [test_runtime_ready.py](../../tests/integration/test_runtime_ready.py) L199 通过把 lease_until 改成过去模拟过期；L503 有独立 worker 子进程、两个 Fake 并发槽。 | 在实际独立进程及 HTTP 流中做断开／终止／重启演练，核对重复收费、产物丢失与恢复边界；不能把手工改时间等同于真实杀进程验收。 |
| A09 证据类型与失败范围 | 部分实现 | [test_state_api.py](../../tests/integration/test_state_api.py) L251–305 验证采用与证据分开、局部／数值证据不升级一般证明；失败四分类 UI 已有。 | G05 的精确目标与证据范围绑定，以及模型失败结果的对应记录。 |
| A10 旧审查与异议 | 版本规则核心离线通过；真实独立审查待验 | [test_state_api.py](../../tests/integration/test_state_api.py) L361、L398；[test_runtime_ready.py](../../tests/integration/test_runtime_ready.py) L285 固定版本及局部审查落库。 | 后一测试虽名为 test_real_review，但用的是 MockProvider。需要真实审查调用和独立数学核对，不能仅看生成模型／同模型再次同意。 |
| A11 预算竞争、重试、子任务上限 | 请求台账核心通过；全项未齐 | [test_runtime_ready.py](../../tests/integration/test_runtime_ready.py) L176、L256：并发原子预留、明确未受理才重试、未知占用额度。 | G06/G07：子任务数量／深度／共享额度、分支额度及任务增额恢复。 |
| A12 Hermitian、导出与恢复 | 固定计算／导出／备份核心通过；真实演示未完成 | [test_exports_recovery.py](../../tests/integration/test_exports_recovery.py) L64、L101、L240：实际精确计算、旧依赖导出、ZIP 重开一致、备份恢复。 | 数学叙述是预写，样例本身没有真实研究 Run。须完成方案 §10.1 的真实研究、审查及人工干预演示，保留失败也可，不要求模型一定找出反例。 |

DeepSeek 与 GLM 适配器源码都存在，但只有 DeepSeek 做过一次正常实际调用；GLM 当前尚未配置。施工方案 M3 的“两提供方同一回归集”未通过，需要真实账户条件或明确调整阶段出口，不能将这一项默认为完成。

## 5. 联网能力到底是什么

**数学 worker 当前没有联网搜索、浏览网页、下载资料或执行模型代码的工具。** 适配器没有向模型声明 tools，收到 tool_calls 会拒绝；系统提示也要求只返回符合 schema 的 JSON。既有 [test_remote.py](../../tests/providers/test_remote.py) L95 对请求不包含 tools 有断言。

目前应用执行路径上的网络请求是：

1. 独立 worker 访问本机 HTTP 状态 API，用于领取任务、续租、记账和提交。
2. 真实提供方适配器向配置提供方的 HTTPS `/chat/completions` 发送模型请求。

证据：[remote.py](../../services/api/src/mathagent/providers/remote.py) L21、L104–136、L178、L204；[worker.py](../../services/api/src/mathagent/runtime/worker.py) L225–274。客户端不跟随重定向，不使用代理环境；worker 的状态 API 地址限定为 loopback。

因此，未来可以做到“题目在本地准备，解题时不提供网页检索”。但调用云端模型 API 本身必须联网。当前没有操作系统级断网或出站域名白名单；也不能仅由本地源码证明提供方服务内部的行为。准确表述应是 **不提供检索工具的 API 解题**，而不是完全离线运行。若要求真正离线，需要本地模型后端及相应环境，这不在当前交付中。

本轮只读健康检查：8001 的 API 报告 DeepSeek 已配置且开启；8000 仍是旧进程，报告真实调用未启用。这个差异是当前进程状态，不应视为两个版本有相同配置。本轮没有为审计停止或重启进程。

## 6. 现有测试证据与归档限制

| 证据 | 实际内容 | 能说明什么 |
| --- | --- | --- |
| [python-results.xml](python-results.xml) | 2026-09-08 15:52 开始的 180 项，0 失败／0 错误；XML 时长 42.911 秒，终端摘要约 43.00 秒。 | 现有 Python 用例通过。大量 real 字样指真实本机 HTTP 或模拟真实提供方协议，不代表付费模型实测。 |
| [browser-results.json](browser-results.json) | 2026-09-08 07:06Z 开始，8 项、43.973 秒、无失败。 | 该 JSON 是此前约 44 秒的归档。上一轮后来 38 秒的 8 项回归见终端输出，未更新到同一 JSON，不能把两次结果混为一份归档。 |
| [deepseek-smoke-2026-09-08.json](deepseek-smoke-2026-09-08.json) | 1 次真实请求，`2 + 2 = 4`，输入 678、输出 83、共 761 tokens；产物为 draft。 | 配置、鉴权、正常结构化响应、保存和请求结算已打通。不证明 IMO 能力、复杂研究、独立审查或异常计费通过。 |
| [performance.json](performance.json) | 300 对象、1,000 关系；两个并发命令客户端；回执 p95 415.882 ms，快照 p95 140.234 ms。 | 是 API／SQLite 合成测量。不是两个真实模型 worker，也未测“命令提交到界面更新 2 秒”。 |
| 当前 Git 基线 | `git rev-parse --verify HEAD` 无可解析提交。 | 后续正式验收需要可识别的代码版本及对应报告；不应仅依靠会随工作树覆盖的记录。 |

本轮没有为了审计再次运行整套测试；以上是逐项阅读代码与已存在报告后的证据判断。旧验收文档中的“离线通过”和“等待联调”应按本报告的具体范围理解。

## 7. 后续工作顺序与 IMO 准入

建议先后顺序：

1. **补研究操作和持久步骤**：G01/G02，连通主动项目查询、草稿／证明／失败提交、按需审查及有上限的修订；状态写入继续经过现有版本和权限校验。
2. **补可追溯与可恢复性**：G03/G04/G05/G08，保存实际调用配置、模板、可见输出、失败范围与固定工具证据；确保截断或中断后仍有可检查材料。
3. **补范围与资源交互**：G06/G07/G09，完成分支范围控制、子任务共享预算、增额恢复及约定的一期图操作。
4. **完成第一阶段验收**：真实进程故障演练、真实研究→审查→干预／修订、两适配器回归或明确的范围调整，更新与固定代码版本对应的报告。保留所有失败和限制。
5. **再做 IMO 评测**：将解题结果、审查质量、版本干预与运行恢复分别统计，不能用若干题答对替代工作台验收。

未来 IMO 测试还需记录：确切模型和参数、题面来源、是否给出年份／题号、输入材料、允许工具、每题预算、提示及人工介入。答案应由题面之外的独立解答或确定性检查核对；模型审查不能是唯一评分依据。历史公开题可能已存在于模型训练材料，结果只能作为现有能力和流程的摸底，不能据此声称获得新的数学研究能力。

当前输出上限硬编码为 4096 tokens，输入上限为 200000 字符，没有自动摘要／分块。适配器设置网络阶段超时，但没有整个研究任务的绝对截止计时。长证明可能因截断导致协议失败。审查虽另起一次会话，输入仍可能包含分支中以前的审查产物，未做评测盲审隔离。正式设计题目测试时应先处理或明确记录这些限制。

按照用户本轮选择，**没有选题派发、没有 IMO 结果、没有新增模型消耗**。
