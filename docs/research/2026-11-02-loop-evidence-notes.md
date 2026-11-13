# Loop specification、ReAct、成本与异步输入：证据笔记

核验日期：2026-11-02（Asia/Shanghai）。范围：用户综述引用的论文、DeepSeek Harness（DSH）官方源码和指定讨论帖。本文是研究材料，不改变 Nika 的架构、预算或授权规则。本轮只读取证，未运行 DSH、复现社区案例或执行远端代码。

## 1. 结论与证据等级

| 主张 | 核验结果 | 能说明什么 |
|---|---|---|
| Loop specification 包含 trigger、goal、verification、stopping rule、memory | 论文原文支持 | 作者提出的外部任务规范，不是 harness 内部 ReAct 循环 |
| 50 loops、70%、74% | 已定位论文 §5 | 单一公开目录的人工编码比例，不是任务成功率或行业统计 |
| DSH 有 ReactLoopAgent 与 send/followup/steer/inject | 官方固定版本源码支持 | 输入接纳、唤醒和消费边界可以分别建模；不表示每条消息都有独立输出 |
| 27 步、约 1M tokens、两次认为信息足够仍继续 | 已定位讨论 #2821 | 社区作者的单次故障报告；本轮没有原始 trace 或账单，不是官方基准 |
| 19 sessions、uncached prefill 下降 72–88% | 已定位讨论 #2039 | 作者的 pi 扩展测试自述，非 DSH 官方测试；原始数据只给出本机路径，未独立复核 |
| DSH 支持完整输出外存、预览与恢复入口 | 官方文档和当前代码支持 | spill 是可选能力；不保证任意压缩无损、永久保留或自动取回 |

“已定位报告”不等于“独立复现实测”。

## 2. 论文：研究对象与数字含义

论文为 Sandeco Macedo 的 *Stop Hand-Holding Your Coding Agent: Engineering the Loops that Replace Step-by-Step Prompting*，arXiv:2607.00038v1。提交日期为 **2026-06-28 18:30:46 UTC**，类别 cs.SE。本轮未发现同行评审录用信息。[元数据][paper-abs]

摘要及 §1–3 区分三层：普通程序循环、harness 内部模型与工具循环、交给 harness 的外部 loop specification。外部规范描述何时开始、目标是什么、如何验证、何时停止或求助，以及如何保留跨轮状态。论文并不支持把 ReAct 内核改成持续轮询的自主调度系统。[论文 §1–3][paper]

§5 人工阅读 Loop Library 的 **50 个配方**，编码 trigger、goal type、验证等级、架构、终态和 pattern：

- **70%**：主要验证位于作者定义的 level 1/2，即确定性验证或规则验证。
- **74%**：配方写出了命名终态。

它们不表示“70% 的任务自主成功”或“74% 正确停止”。[论文 §5][paper-s5]

§9 明确限定为 position paper + descriptive corpus study：单一目录、非随机样本、人工定性判断，部分歧义项有推断，不作因果结论。作者提供的 skill 也不是经过效果评估的系统。本轮没有重新编码全部 50 项，只确认论文方法和汇总数字存在。[论文 §9][paper-s9]

对 Nika 可借用的是“目标、验证、停止/求助、状态保留”的任务契约。外部 loop specification 与内部 AgentLoop 不是同一层，论文建议的 hard budget ceiling 也不会自动覆盖 Nika 的软预算。

## 3. DSH：固定源码版本与运行边界

本轮读取官方 `deepseek-ai/deepseek-harness` 的 `master`，固定为 **ce7073d51254c26cd63b22a60835b8f948deb447**，提交时间 **2026-10-29T16:13:15Z**。[提交][dsh-commit]

### 3.1 内部循环与退出

`ReactLoopAgent.turn()` 的 step loop 位于 `agent.ts:327–426`，处理拒绝进入、空输入、完成、token limit、取消和错误，并在每轮结束前检查 next-step inbox。

`step()` 位于 `agent.ts:566–597`：先提交 assistant message，再处理 `max-tokens`、无 tool-call block，以及执行 Tool 后的 `result.concluded`。Tool 追加的内容可进入 next-step inbox 并驱动后续 Step。[turn 实现][dsh-turn]、[step 出口][dsh-step]

本轮检查的核心类和配置未见每 Turn `maxSteps` 计数闸，明确存在的是 Tool batch 内的 `maxParallelToolCalls`。但这不证明所有 DSH 组合都没有预算：pre-step、取消、模型错误和插件策略仍可介入，本轮未枚举全部插件和部署配置。[核心配置][dsh-config]、[`pre-step`][dsh-prestep]

因此，“无 Tool Call 后 completed”是执行协议出口，不是外部产物验收。模型说信息足够也不是独立验证，更不应通过解析隐藏 CoT 决定停止。

### 3.2 Inbox、唤醒与异步输入

官方 `agent.ts:170–188` 的映射为：

| API | 目标 | 是否唤醒 |
|---|---|---|
| `send(message, target, wakeup)` | 调用者指定边界 | 调用者指定 |
| `followup(message)` | next-turn | 是 |
| `steer(message)` | next-step | 是 |
| `inject(message)` | next-step | 否 |

`send` 先写 inbox；`wakeDriver` 遇到活跃 driver 时不创建第二个 driver，活跃 driver 在后续边界消费 Pending。`preStep` 才 claim 输入。`inbox.ts` 通过持久 splice 事件重建 next-step/next-turn，并检查非法 splice 和重复 message ID。[输入与唤醒][dsh-input]、[inbox projection][dsh-inbox]

官方还明确：followup 只是 enqueue，message ID 证明接纳，不证明随后某条 assistant response 是该消息独占的结果。`whenIdle` 观察整个 Agent 生命周期，SDK 的 RunResult 需要调用者明确拥有一个 activity interval。[官方 follow-up ownership 决定][dsh-followup]

这证明“输入接纳”和“执行串行”可以分离，不要求 Nika 复制 next-turn/next-step 两种产品入口。Nika 的统一 steer 契约应保留。

### 3.3 持久 Session 与活跃 Agent

DSH 区分 append-only Session log、live Agent handle、create/resume 和持久存储挂载。源码 constructor 初始为 idle，恢复、启动和唤醒各有入口。持久记录不等于常驻执行，也不证明历史副作用会自动继续。[核心职责/创建][dsh-core]、[Session 持久化边界][dsh-session]、[Agent 初态与唤醒][dsh-input]

是否重启唤醒、恢复哪些 Pending Input、如何处理未知 Tool 结果，仍是显式策略。

### 3.4 Spill 与 Compaction 是不同机制

当前 spill 文档规定：保存调用者提供的完整文本，返回 opaque locator、byte count 和 retrieval hint。存储失败必须拒绝，由消费方决定降级。spill 可选，不属于 Loop 核心；消费方负责预览和裁剪，存储层不提供通用检索 API，也不承诺永久保留。[Spill 文档][dsh-spill]

当前 `spill-policy` 使用 `maxInlineTokens`，省略则不启用 retention。缺少存储或图片计价信息时保留原内容并报告原因。它已不同于 8 月讨论 #2039 的 `maxInlineBytes=50000` 快照，旧阈值不是当前默认值。[当前 policy][dsh-spill-policy]

可复用原则是：保存完整结果，并在模型上下文中保留可用预览和恢复入口。它解决“上下文太大但不能丢证据”，不解决“模型不肯结束”。入口失效、GC 和模型未主动读取仍会影响恢复。

## 4. 指定讨论帖核验

| 帖子 | 实际内容与日期 | 本轮能确认和不能确认的内容 |
|---|---|---|
| [#2821][d2821] | syukouyuu，2026-08-17：ReactLoopAgent 未设 step 上限；一次探索问答 27 steps/~103s，累计约 1M tokens | 原文确有这些数字，模型在 step 9/10、22/23 表示信息足够仍调工具。回复者另称核过旧 `99f6f02fec`。本轮没有原始 Session/usage 明细和独立重跑，只作事故报告，不能据此断定第 9 或 22 步已能正确回答 |
| [#2039][d2039] | adamteale，2026-08-15：spill-copy-on-prune 提案 | 原文称 19 live sessions（glm-5.3 + deepseek-v4-flash）、uncached prefill 下降 72–88%、transcript chars 下降 81–93%。对象是其 **pi extension**；代码和 benchmark 仅指本机目录，没有公开可复现数据包。本轮确认主张存在，未核验数值、配对方法或 correctness parity |
| [#3228][d3228] | Qian12581，2026-08-19：flash 反复搜索直到欠费，称消耗一亿多 tokens | 存在用户报告和一段模型继续搜索文字，但没有完整逐次 usage 或账单。不作为普遍成本量级或精确测量 |
| [#961][d961] | ciceroyang，2026-08-14：开发报告插件教程 | 是插件作者经验，不是 DSH loop 效率或异步协议基准 |
| [#9368][d9368] | SunXuehang，2026-10-10：Windows Desktop 因旧 dsh-tools 依赖启动失败 | 是包版本和服务加载故障报告，不是 ReAct 失控、消息能力或 token 成本证据；评论中的源码解释也不等于维护者裁决 |

“官方仓库中的 Discussion”只表示托管地点。作者报告、第三方分析、维护者确认和独立复现是不同证据等级。

## 5. 不成立或需要条件的泛化

1. **串行 Tool 不等于无法接收消息。** 串行描述副作用顺序，ingress 可以先接纳，在安全边界再注入。DSH inbox/send 和 Nika steer 都采用这种分离。
2. **持久 Session 不等于常驻 Agent 或自动续跑。** 数据存在、运行实例存在、可重建上下文、可继续未知副作用是四个不同承诺。
3. **平方增长是有条件的累计输入模型。** 若每步输入为 `I_k = B + (k-1)d`，完整历史重送且没有压缩或裁剪，则 N 步累计输入为 `NB + dN(N-1)/2`。这不等于账单平方增长。实际费用还受缓存价格、输出、变长 Tool 结果、Compaction、上下文上限和重试影响。当前源码使用 `deriveMessages()` 构造投影，也不能说永远发送所有原始轨迹。[当前 request 构造][dsh-request]
4. **模型 stop/end_turn 不等于业务成功。** 它可能只是没有下一 Tool Call，或触及长度、取消、错误边界。终态、产物、验证和交付需要分别保留。
5. **Compaction 不替代执行出口。** 它可以缓解上下文压力，但不能证明任务完成或没有进展。Step/费用上限也不保证上下文合法。是否新增硬门是产品选择。
6. **异步入队回执不等于任务结果。** received/accepted/injected、当前执行、最终响应和外部验收必须分开；新输入可能改变同一 activity。
7. **Spill 保存原文不等于全系统零损失。** 还需核对持久化、入口有效性、保留策略、跨 Session 访问和重新读取。19 sessions 的作者报告不能证明所有任务等价。

## 6. 对本次接手的有限建议

这些证据支持把模型响应分类、Tool 可执行性、Tool Result 回灌、继续/结束判定和输入安全边界写成精确协议，同时保留按任务目标验证的语义层。无需强制解析 `Thought:`，也无需新增 Reasoner/Planner 框架。

这些数字不进入 Nika 的验收承诺。实际测量应固定模型、版本、工具、任务和缓存条件，分别记录累计输入、cached/uncached 输入、输出、费用、被接受的结果、重复行动和终止原因。本轮未开展此实验。

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
