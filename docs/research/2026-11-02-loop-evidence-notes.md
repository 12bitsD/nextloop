# Loop specification、ReAct、成本与异步输入：证据笔记

核验日期：2026-11-02（Asia/Shanghai）。范围：用户综述引用的论文、DeepSeek Harness（DSH）官方源码及指定讨论帖。只读联网取证；未运行 DSH、未复现社区案例、未执行远端代码。本文件是研究材料，不改变 Nika 的已确认架构、预算或授权规则。

## 1. 结论与证据等级

| 主张 | 本轮结果 | 可支持的范围 |
|---|---|---|
| loop specification 包含 trigger、goal、verification、stopping rule、memory | 论文原文支持 | 作者提出的外部任务规范；明确区别于 harness 内部 ReAct 循环 |
| 50 loops、70%、74% | 已定位论文 §5 | 单一公开目录的人工编码比例；不是任务成功率、成本实验或全行业统计 |
| DSH 有 ReactLoopAgent 与 send/followup/steer/inject | 官方固定版本源码支持 | 接纳输入、唤醒、消费边界分别建模；不能推论每条消息对应一个独立输出 |
| 27 步、约 1M tokens、两次认为信息足够仍继续 | 已定位讨论 #2821 | 社区作者的单次故障报告；原始 trace/账单未在本轮取得，不是官方基准 |
| 19 sessions、uncached prefill 下降 72–88% | 已定位讨论 #2039 | 作者的 pi 扩展测试自述，非 DSH 官方测试；原始数据只给出本机路径，本轮未独立复核 |
| DSH 支持完整输出外存、预览与恢复入口 | 官方文档及当前代码支持 | spill 是可选能力；不等于任意压缩都无损，也不保证永久保留或自动取回 |

来源分别见下节；“已定位报告”与“独立复现实测”严格区分。

## 2. 论文：研究对象与数字含义

论文为 Sandeco Macedo 的 *Stop Hand-Holding Your Coding Agent: Engineering the Loops that Replace Step-by-Step Prompting*，arXiv:2607.00038v1。arXiv 页面显示提交日期为 **2026-06-28 18:30:46 UTC**，类别 cs.SE；本轮未发现该页面提供同行评审录用信息。[元数据][paper-abs]

摘要及 §1–3 区分三层：普通程序循环、harness 内部模型与工具的执行循环、交给 harness 的外部 loop specification。后者描述何时开始、追求什么、如何实际验证、何时停止/求助，以及怎样留下跨轮状态；正文同时明确执行阶段。不能据这篇论文声称 ReAct 内核必须改成一个持续轮询的自主调度系统。[论文 §1–3][paper]

§5 人工阅读 Loop Library 的 **50 个配方**，编码 trigger、goal type、主要验证等级、架构、终态和 pattern。其 **70%** 指主要验证位于作者定义的 level 1/2（确定性验证/规则验证）；**74%** 指配方写出命名终态。它们不是“70% 的任务自主成功”或“74% 正确停止”。[论文 §5][paper-s5]

§9 明确限定：position paper + descriptive corpus study；单一目录、非随机样本、人工定性判断，歧义项有推断，不作因果结论。作者提供的 skill 也不是经过效果评估的系统。本轮没有重新编码全部 50 项，验证到的是论文方法与汇总数字存在且含义明确。[论文 §9][paper-s9]

**对 Nika 的适用解释：** 可以借用“目标、验证、停止/求助、状态保留”的任务契约问题；不能把外部 loop specification 与内部 AgentLoop 混为一层，也不能把论文建议的 hard budget ceiling 自动覆盖 Nika 已确认的软调用/时间预算。

## 3. DSH：固定源码版本与运行边界

本轮读取官方 `deepseek-ai/deepseek-harness` 的 `master`，固定为 **ce7073d51254c26cd63b22a60835b8f948deb447**，提交时间 **2026-10-29T16:13:15Z**。[提交][dsh-commit]

### 3.1 内部循环及出口

`ReactLoopAgent.turn()` 的 step loop 在 `agent.ts:327–426`。它处理拒绝进入、空输入、完成、token limit、取消和错误；每轮结束前还检查 next-step inbox。`step()` 在 `agent.ts:566–597` 先提交 assistant message，再区分 `max-tokens`、没有 tool-call block、以及执行 Tool 后的 `result.concluded`。Tool 追加上下文进入 next-step inbox，可驱动后续 step。[turn][dsh-turn]、[step 出口][dsh-step]

本轮检查的核心类/配置未见内置每 Turn `maxSteps` 计数闸；当前配置明确存在的是一个 Tool batch 内的 `maxParallelToolCalls`。但不能据此断言所有 DSH 组合都没有预算：pre-step 可拒绝，取消、模型错误及插件策略均可介入。本轮没有枚举所有插件与部署配置。[核心配置][dsh-config]、[pre-step][dsh-prestep]

**机制意义：** “无 tool call 后 completed”是执行协议出口，不是外部产物验收。模型正文或公开推理摘要说信息足够，也不构成独立验证，更不应成为解析隐藏 CoT 的停止逻辑。

### 3.2 inbox、唤醒与异步输入

官方 `agent.ts:170–188` 的映射为：

| API | 目标 | 是否唤醒 |
|---|---|---|
| `send(message, target, wakeup)` | 调用者指定边界 | 调用者指定 |
| `followup(message)` | next-turn | 是 |
| `steer(message)` | next-step | 是 |
| `inject(message)` | next-step | 否 |

`send` 先写 inbox；`wakeDriver` 遇到正在运行的 driver 不创建第二 driver，活跃 driver 在后续边界消费 pending input。`preStep` 才 claim 输入。`inbox.ts` 通过持久 splice 事件重建 next-step/next-turn，检查非法 splice 和重复 pending message ID。[输入与唤醒][dsh-input]、[inbox projection][dsh-inbox]

官方设计说明还专门强调：followup 只是 enqueue，message ID 证明接纳，不证明随后某条 assistant response 是该消息独占的结果；`whenIdle` 是整个 Agent 的生命周期观察。SDK 的 RunResult 需要调用者明确拥有一个 activity interval，不能用一个入队回执冒充任务完成。[官方 follow-up ownership 决定][dsh-followup]

**与 Nika 的边界：** 该代码证明“输入接纳”和“执行串行”可以分离，不要求 Nika 复制 DSH 的 next-turn/next-step 两种产品入口。Nika 当前已确认普通输入统一 steer，应保留自己的契约。

### 3.3 持久 Session 与活跃 Agent

DSH 文档区分 append-only Session log/in-memory store、live Agent handle、create/resume 和持久存储挂载。源码 constructor 初始是 idle，恢复、配置启动与后续唤醒各有独立入口；持久记录本身不等于常驻执行，也不证明历史副作用自动继续。[核心职责/创建][dsh-core]、[Session 持久化边界][dsh-session]、[Agent 初态与唤醒][dsh-input]

尤其不能从“能恢复历史”泛化为“所有中断操作可自动恢复”。是否重启后唤醒、恢复什么 pending input、怎样处理未知 Tool 结果，仍是显式策略。

### 3.4 spill 与压缩是不同机制

当前官方 spill 文档规定：保存调用者提供的完整文本，返回 opaque locator、byte count 与 retrieval hint；存储失败必须拒绝，由消费方决定降级。spill 可选，不属于 loop 核心；消费方负责预览/裁剪策略，存储层不提供通用检索 API，也不定义永久保留。[Spill 文档][dsh-spill]

当前 `spill-policy` 使用 `maxInlineTokens`，省略则不启用 retention；存储/图片计价等缺失时保留原内容并报告原因。这已不同于 8 月讨论 #2039 的 `maxInlineBytes=50000` 快照，不能把旧阈值当当前默认。[当前 policy][dsh-spill-policy]

**可复用原则：** 把完整结果持久化，并在模型上下文保留可用预览与恢复入口。它解决“上下文太大又不能丢证据”，不直接解决“模型一直行动、不肯结束”。入口失效、GC 和模型未主动读取，都可能影响实际可恢复性。

## 4. 指定讨论帖核验

| 帖子 | 实际内容与日期 | 本轮能确认什么 / 不能确认什么 |
|---|---|---|
| [#2821][d2821] | syukouyuu，2026-08-17：ReactLoopAgent 未设 step 上限；一次探索问答 27 steps/~103s，累计约 1M tokens | 原文确有这些数字；模型在 step 9/10、22/23 表示信息足够仍调工具。回复者另称核过旧 `99f6f02fec`。本轮没有原始 session/usage 明细和独立重跑，因此只作事故报告；不能说已客观证明第 9 或 22 步就足够正确回答 |
| [#2039][d2039] | adamteale，2026-08-15：spill-copy-on-prune 提案 | 原文确称 19 live sessions（glm-5.3 + deepseek-v4-flash）、uncached prefill 下降 72–88%、transcript chars 下降 81–93%。实验对象是其 **pi extension**；代码/benchmark 仅指本机目录，正文未给可复现公开数据包。本轮确认主张存在，未独立核验数值、配对方法或 correctness parity |
| [#3228][d3228] | Qian12581，2026-08-19：flash 反复搜索直到欠费，称消耗一亿多 tokens | 存在该用户报告和一段模型继续搜索文字；无完整逐次 usage/账单可供本轮复核。不作为普遍成本量级或精确测量 |
| [#961][d961] | ciceroyang，2026-08-14：开发报告插件的教程 | 是插件作者经验，不是 DSH loop 效率或异步协议基准；不用于支撑上述数字 |
| [#9368][d9368] | SunXuehang，2026-10-10：Windows Desktop 因旧 dsh-tools 依赖启动失败 | 是包版本/服务加载故障报告。不是 ReAct 失控、消息接收能力或 token 成本证据；评论中的源码解释也不等于官方维护者裁决 |

“官方仓库中的 Discussion”表示托管地点；作者报告、第三方分析、维护者确认和独立复现是不同证据等级。

## 5. 不成立或需要条件的泛化

1. **串行 Tool 不等于运行中无法接收消息。** 串行描述副作用派发顺序；ingress 可以先接纳，安全边界再注入。DSH 的 inbox/send 是源码例证，Nika 的 steer 也已采用这一分离。
2. **持久 Session 不等于常驻 Agent 或自动续跑。** 数据存在、运行实例存在、可重建上下文、可继续未知副作用是四个不同承诺。
3. **平方增长是有条件的累计输入量模型。** 若每步输入为 `I_k = B + (k-1)d`，完整历史重送且没有压缩/裁剪，则 N 步累计输入为 `NB + dN(N-1)/2`。这是近似 `O(N²)` 的条件推导；不能直接叫“账单平方增长”。实际费用还受 prefix cache 命中及价格、输出/reasoning tokens、变长工具结果、压缩、上下文上限与重试影响。当前源码使用 `deriveMessages()` 生成投影，也不能直接说永远发送所有原始轨迹。[当前 request 构造][dsh-request]
4. **模型 stop/end_turn 不等于业务成功。** 它可能只是没有下一工具调用，或触及长度/取消/错误边界。终态、产物、验证和交付应分别保留。
5. **compaction 不替代执行出口。** 降低上下文压力可以让循环继续更久，但无法独自证明工作完成或没有进展。反过来，step/费用上限也不保证上下文合法。Nika 的软预算是否要新增硬门是产品选择，研究材料不自动授权改变。
6. **异步入队回执不等于异步任务结果。** 应区分 received/accepted/injected、当前执行、最终响应和外部验收；新输入可能改变同一 activity，不能假设一条消息自然拥有一份最终结果。
7. **spill 保存原文不等于全系统“零损失”。** 要同时核对持久化成功、入口有效、保留策略、跨 Session 访问和重新读取；19 sessions 的作者报告不能证明所有任务表现等价。

## 6. 对本次接手的有限建议

这些证据支持把模型返回分类、Tool 调用可执行性、Tool Result 回灌、结束/继续判定及输入安全边界写成精确协议；同时保留按任务目标验证的语义层。无需为此强制解析 `Thought:`，也无需新增 Reasoner/Planner 框架。

数字不进入 Nika 的验收承诺。后续若做实际测量，应固定模型、版本、工具、任务和缓存条件，分别记录累计输入、cached/uncached 输入、输出、费用、被接受的结果、重复行动与终止原因；本轮不开展此实验。

[paper-abs]: https://arxiv.org/abs/2607.00038
[paper]: https://arxiv.org/html/2607.00038v1
[paper-s5]: https://arxiv.org/html/2607.00038v1#S5
[paper-s9]: https://arxiv.org/html/2607.00038v1#S9
[dsh-commit]: https://github.com/deepseek-ai/deepseek-harness/commit/ce7073d51254c26cd63b22a60835b8f948deb447
[dsh-turn]: https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/packages/core/agent-loop/src/agent.ts#L327-L426
[dsh-step]: https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/packages/core/agent-loop/src/agent.ts#L566-L603
[dsh-config]: https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/packages/core/agent-loop/src/index.ts#L250-L307
[dsh-prestep]: https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/packages/core/agent-loop/src/agent.ts#L283-L316
[dsh-input]: https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/packages/core/agent-loop/src/agent.ts#L140-L250
[dsh-inbox]: https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/packages/core/agent-loop/src/inbox.ts#L19-L112
[dsh-followup]: https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/.agents/notes/implemented/architecture/2026-07-30-followup-enqueue-and-owned-runs.md
[dsh-core]: https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/docs/subsystems/core.md#L7-L55
[dsh-session]: https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/docs/subsystems/session.md#L1084-L1087
[dsh-spill]: https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/docs/subsystems/spill.md#L5-L92
[dsh-spill-policy]: https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/packages/spill/spill-policy/src/index.ts#L25-L105
[dsh-request]: https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/packages/core/agent-loop/src/agent.ts#L726-L742
[d2821]: https://github.com/deepseek-ai/deepseek-harness/discussions/2821
[d2039]: https://github.com/deepseek-ai/deepseek-harness/discussions/2039
[d3228]: https://github.com/deepseek-ai/deepseek-harness/discussions/3228
[d961]: https://github.com/deepseek-ai/deepseek-harness/discussions/961
[d9368]: https://github.com/deepseek-ai/deepseek-harness/discussions/9368
