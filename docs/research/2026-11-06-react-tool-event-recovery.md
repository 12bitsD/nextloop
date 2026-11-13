# ReAct 响应决策、Tool 恢复与事件持久化：一手源码依据

> **一手源码证据与当时的方案建议。**后续架构已定稿为 [DSH-aligned AgentLoop](../design/agent-loop.md) 和 [ADR-0002](../adr/0002-dsh-aligned-agent-loop.md)：采用 DSH Turn/Step 与并行调度。本文保留研究时的自定义事件和串行建议作为备选分析，不作为当前契约。

核验日期：2026-11-06（Asia/Shanghai）。范围：模型响应如何成为动作、Tool 校验失败如何修正、unknown outcome 如何关批，以及 append-only Event 如何支持崩溃恢复。本文提供 **reference + self-build** 依据，不重新讨论仓库拆分，也不建议引入 Agent 框架、Runner、图执行器或通用 EventBus。

证据分三类：**参考项目事实**来自固定版本官方源码；**我们的推论**是由这些事实导出的设计判断；**建议**仍是当时的 proposed 方案，不代表已经实现。本轮未安装或运行这些项目。“源码有测试”只表示测试存在，不表示本轮运行通过。

## 1. 结论摘要

1. **先分类响应，再决定动作。** Provider 流先组装成逻辑响应，再区分文本、Tool Calls、混合、空、refusal、截断、中断和协议错配。只有完整且整批校验通过的 Tool Calls 才可执行。无 Tool Call 只产生 final candidate，不证明业务成功。
2. **校验拒绝与执行失败是两条路径。** 任一格式、身份、名称或 Schema 错误：整批零执行，记录拒绝，再通过 Provider 合法投影做一次有界修正。普通执行错误提交真实结果并继续同批。未知副作用关批，不参与这两种默认续行。
3. **恢复是补全事实，不是续跑旧指令。** Intent 先提交，Result 后提交。只有 Intent 而没有 started/result 证据时，不能声称确定未执行；started 后无 result 必须保留 unknown。恢复只重建 Projection、关闭旧 Run、保留资源限制和恢复已提交 final 的投递。
4. **DSH 是 Session/Projection/结束检查/crash closer 的首要参考；Hermes 是有界恢复、steer 和 finalization 的次要参考。** PydanticAI 与 OpenAI Agents SDK 只用于 Provider/Schema/Fake-test 缝，其 Runner、durable replay、逐项反馈和消息 Session 不能直接成为 nextloop 内核。

对应基线：[AgentLoop](../design/agent-loop.md)、[Loop interface](../design/loop-interface.md)、[Requirements](../architecture/requirements.md)、[Roadmap](../architecture/discussion-roadmap.md)。

## 2. 固定版本与使用范围

| 项目 | 固定官方版本 | 本文使用范围 |
|---|---|---|
| DSH | [`deepseek-ai/deepseek-harness@fcc0ad4`](https://github.com/deepseek-ai/deepseek-harness/commit/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159) | Session、Surface Projection、ReactLoopAgent、Tool batch、repair |
| Hermes | [`NousResearch/hermes-agent@ca43606`](https://github.com/NousResearch/hermes-agent/commit/ca4360692055bdb4df3a695c164646e528891da8) | 原生 Python driver、文本出口、steer、finalizer 和测试 |
| PydanticAI | [`pydantic/pydantic-ai@4ee8492`](https://github.com/pydantic/pydantic-ai/commit/4ee84920d36d5f71c16c08fed320a935c951f2bb) | Schema、预校验、retry feedback、TestModel、persistence 边界 |
| OpenAI Agents SDK | [`openai/openai-agents-python@d4f07df`](https://github.com/openai/openai-agents-python/commit/d4f07df2915ddb029d7076595bb7d4ce89322308) | Model 接口、converter、function schema、ScriptedModel |

行号链接均定位到上述 SHA，后续上游变化不自动进入本文结论。

## 3. 参考项目事实

### 3.1 DSH：唯一日志、派生历史与保守关闭

**Session 是 append-only Event log，模型历史是派生结果。** seed 恢复时检查 lossless JSON 和连续 `seq`，`deriveMessages()` 从当前 Surface 生成历史。[Session 模块与类][dsh-session]、[恢复校验][dsh-seed]、[deriveMessages][dsh-derive]

**Projection 不是逐事件拼消息。** `foldSurface()` 顺序解释 append/replace/projection；`deriveEventMessage()` 只映射 Surface 事件，turn/step、attempt 等 log-only 事实不进入模型历史。未知必需事件默认使重建失败，只有显式 `ignorable` 的未知事件才可跳过。[surface fold][dsh-surface]、[事件兼容规则][dsh-event]

**结束候选之后还有输入检查。** `ReactLoopAgent.turn()` 在已有结束原因且 next-step 为空时运行 stopping hook，随后再次检查 next-step；仍为空才退出，否则继续同一 Turn。`steer()` 写 next-step，`followup()` 写 next-turn。[结束检查][dsh-ending]、[输入入口][dsh-input]

**响应和 Tool 生命周期有持久顺序。** 完整 assistant message 先 append；`BatchSettlement.start()` 在 dispatch 前 append `tool/call`，`commit()` 追加引用 call seq 的 `tool/result`。取消会为 skipped calls 补关联结果。[响应与派发][dsh-response]、[intent/result][dsh-intent]

**崩溃修复只追加关闭事实。** `ToolCallRecovery` 区分没有 `tool/call` 的 `TOOL_NOT_STARTED` 与已有 call、无 durable result 的 `TOOL_OUTCOME_UNKNOWN`；`interruptedTurnClosers()` 补结果、step/end 和 interrupted turn/end，不重新执行旧 Tool。[修复入口与分类][dsh-repair]、[closer 生成][dsh-closers]

边界：DSH 的 `tool/call` 同时是“已记录开始”证据；nextloop 若另有 intent/started 两级事件，就不能照搬其 not-started 推断。DSH 对坏 JSON 保留字符串并交给 Tool 自己诊断，不是整批 Schema 准入，也没有 nextloop 的 unknown 关批规则。[DSH 参数解析][dsh-parse]

### 3.2 Hermes：响应恢复、steer 移交与统一 finalizer

**无 Tool Call 进入独立文本出口。** `finish_text_response()` 返回 `continue / break / return`；空内容、reasoning-only、短促“还将继续”、退化尾部、`finish_reason=tool_calls` 却空调用等，各有有界恢复或 stop gates。正常 break 后仍进入 `finalize_turn`。[driver 实现][h-driver]、[文本出口][h-text-exit]

这些是 Hermes 的具体 heuristic 和插件规则，不是通用业务完成判定。reasoning-only promotion 依赖 route capability，不构成把隐藏推理当用户答案的通用许可。[reasoning 分支][h-reasoning]

**steer 是进程内 buffer，finalizer 负责 leftover 移交。** `steer()` 在锁内追加文本；Tool/下一次 API 边界可注入。finalizer 将无 ticket 的 leftover 作为 `pending_steer` 返回下一 Turn，ticketed 输入保留给原 settlement。[steer 实现][h-steer]、[leftover 移交][h-leftover]

**finalization 分项容错。** `_guarded_cleanup()` 独立保护 Trajectory、资源清理和持久化，错误进入 `cleanup_errors`；测试逐项注入异常并检查后续步骤仍执行。[finalizer 实现][h-finalizer]、[清理测试][h-cleanup-test] gateway 另有“清理 await 期间迟到输入不丢失、不创建重复 owner”的竞态测试。[竞态测试][h-race-test]

边界：Hermes 的 steer 锁不是 durable inbox，finalizer 也不是 nextloop 所需的事件事务；“出错仍返回响应”不能覆盖必需事实未提交。它把 final 后 leftover 交下一 Turn，而 nextloop 选择 final commit 前已接纳 steer 继续当前 Turn。Hermes hard stop 清未消费 steer，也不适用于 nextloop。[hard/soft clear][h-clear]

### 3.3 PydanticAI：Schema 与 retry feedback，不采用其批次范围

`function_schema()` 同时构建 JSON Schema 和 validator；`ToolManager.validate_tool_call()` 在执行前解析名称、校验参数，并把 `ValidationError/ModelRetry` 转为带预算信息的 `ToolRetryError`。校验与执行分别由 `validate_tool_call()` 和 `execute_tool_call()` 表达。[schema 构建][p-schema]、[校验入口][p-validate]

`_validate_function_calls()` 会在本批执行前拒绝重复 `tool_call_id`；但普通参数失败形成该项 retry feedback，合法兄弟项仍执行。执行器支持 parallel 和 sequential，顺序配置本身不提供 intent commit、unknown 关批或资源限制。[重复 ID][p-duplicate]、[校验后执行][p-after-validation]、[执行策略][p-execution]

`TestModel` 可生成符合 Schema 的参数、记录请求并模拟普通/流式响应，适合作为 deterministic Provider 测试参考。[TestModel][p-test-model]

边界：PydanticAI 将 conversation、StepPersistence 和 durable execution 分开；durable execution 包括 replay 已完成 step、重试进行中 step。这与 nextloop“恢复事实但不自动重放未决副作用”不同。[持久化分类][p-persistence]

### 3.4 OpenAI Agents SDK：Provider-neutral seam 与脚本化测试

`Model.get_response()/stream_response()` 是统一模型边界，`ModelProvider` 只负责按名称解析 Model；接口要求非空 call ID 在 Run 及序列化恢复谱系中稳定关联 Tool 调用与输出。[Model 接口][o-model]

`function_schema()` 从 Python 函数生成 Pydantic 参数模型和 JSON Schema，可再转换为 strict schema。`Converter.message_to_output_items()` 将 Chat Completions 的文本、refusal、reasoning 和 Tool Calls 归一化；`items_to_messages()` 处理反向投影、call/output 配对和 Provider 私有 reasoning 数据。[function schema][o-schema]、[Chat converter][o-converter]

`ScriptedModel` 提供确定性 `ModelStep`、精确 stream events、错误/retry advice、请求快照，以及“额外调用”和“脚本未消费完”的断言。这比只 mock 最终文本更适合验证响应到动作的协议。[ScriptedModel][o-scripted]

边界：这些是可借鉴的 Adapter/test 形状。`Runner` 自己推进 turn 和 `NextStepFinalOutput`；`Session` 是可增删消息存储，不是 append-only Event 权威；`RunState` 恢复也不替代 nextloop 的事件恢复。[Runner 主循环][o-runner]、[final 分支][o-final]、[Session][o-session]

## 4. 我们的推论：nextloop 应自研的不变量

1. **逻辑响应与 wire 响应分开。** Adapter 输出完整性、终止原因、文本/refusal、Tool Calls、usage 和 reasoning/opaque 字段；driver 只消费逻辑事实。wire 类型不得进入公共 Event envelope。
2. **记录、准入、执行、交付是四个事实。** 保存无效响应不等于允许执行；合法 Tool Result 不等于任务成功；完整回答不等于 final commit；final commit 不等于已经送达。
3. **同批串行只定义顺序，不定义依赖。** 参数依赖或“是否执行取决于前项成功”的动作必须跨模型调用。ordinary error 续批、validation rejection 零执行、unknown 关批不能合并成一个 `is_error` 分支。
4. **恢复能力取决于提交粒度。** 若只有 intent 而没有独立 started 事实，最多能证明“未能确认是否开始”，不能安全归类为 not-started。增加 `tool_execution_started` 只是缩小不确定性，不证明副作用已经发生或没有发生。
5. **Projection 可以排除非法 wire，但不能篡改历史。** 原始响应和诊断永久保留；下一请求只使用 Provider 合法投影。恢复和在线构建复用同一确定性规则。

## 5. 建议：响应 → 动作决策表

优先级统一为：**用户取消 / lease 失效 / 必需提交失败 > 下表普通分支**。所有“继续”都要求 Run 所有权有效、必要事实已提交且没有禁止继续的 unknown 资源。`new model_call` 是同 Turn/Run 内的新逻辑请求，不是 Provider exact-byte retry。

| Provider 逻辑响应 | 先提交的事实 | Driver 建议动作 | 明确禁止 |
|---|---|---|---|
| 请求失败，尚无 assistant output | attempt 失败、usage（如有）、是否有新输入 | 仅在无输出、无新输入且 retry policy 允许时，以同 `model_call_id` 做字节一致 attempt；否则进入 finalization | 已改请求仍沿用旧 attempt；无限重试 |
| 流中断或连接失败，已有 partial output | partial response、失败与已组装片段；标为不完整 | 不做 exact-byte retry；Provider 能合法承接时做一次有界 continuation/correction 新调用，否则收尾 | 执行截断尾部之前的“完整子集”；把 partial 当 final |
| 完整、无 Tool Calls、有文本 | 完整 assistant response + usage | 生成 final candidate；串行检查 Pending steer，有则注入续调，无则 final commit → WindingDown | 以 `finish_reason=stop` 证明业务成功；重复 append final 文本 |
| 完整 refusal、无 Tool Calls | refusal 类型、完整响应与 usage | 保留 refusal 语义，仍走 Pending-steer/final-commit 边界；诊断不标成业务成功 | 转成空响应无限重试；伪造普通成功文本 |
| 完整 Tool Calls，无正文 | 完整 response + usage | 整批完整性/身份/名称/参数/Schema 校验；全过才提交 intents 并按模型顺序执行 | 校验前执行第一项；从 reasoning 文本补调用 |
| 完整混合正文 + Tool Calls | 同一 assistant response 中保留正文与调用顺序 | 正文是过程事实，不单独 final delivery；按整批 Tool 路径继续 | 丢弃正文；先发 final 再执行 Tool |
| 完整空响应 | 空响应、usage、Provider 终止原因 | 记录协议异常；允许一次有界 correction 新调用，仍为空则诚实收尾 | 将空文本作为成功 final；原样无限重试 |
| `finish_reason=tool_calls` 等信号与空调用数组矛盾 | 协议错配与实际响应 | 按不可执行响应做一次有界 correction；不进入 ToolRuntime | 根据 finish reason 发明 Tool Call |
| 长度截断、Provider `incomplete`、尾部 call 未闭合 | 不完整状态与完整原始事实 | 整批零执行；可做一次有界 continuation/correction，新的响应重新完整校验 | 自动拼接成看似完整的调用；执行已完整的前缀 calls |
| 完整响应但名称、JSON、Schema、ID 或身份关联无效 | response + batch rejection + 可关联的逐项处置 | 零执行；构造 Provider 合法反馈，满足第 6 节条件时做一次 correction | 修名、修 JSON、猜参数、执行合法兄弟项、伪造 ID |
| Tool 批全部完成且允许继续 | 每个 call 的 result 和 batch 完成事实 | 注入完整 Observation 批，进入下一逻辑模型调用 | 隐藏 ordinary error；自动重试失败项 |
| 任一 Tool unknown | 已完成结果、当前 unknown、未启动项 not-executed、资源证据 | 关批；只有 Runtime 允许且执行已退出/隔离时才同 Turn 核查，否则 WindingDown | 续派旧批、盲重放、把 unknown 写成 failure/success |

**修正预算建议：**当时建议每个原始异常响应至多一次自动 correction；修正响应仍无效即 finalization，并记录 `correction_of_model_call_id`。这是一项待采纳策略，不是外部项目的通用定律，不能用新响应重置计数形成隐蔽无限循环。软 Tool budget 独立记录。

## 6. 建议：Tool 校验失败的 Provider 合法恢复

### 6.1 提交流程

```text
完整组装响应
→ 提交 response + usage + completeness/protocol facts
→ 对完整批次做纯校验（不调用 Tool）
→ 任一失败：原子提交 batch_rejected 与所有可关联项的 disposition
→ 检查 correction admission
→ 构造并 preflight Provider 合法的新请求
→ 新 model_call_id，同 Turn/Run 修正一次
```

`batch_rejected` 至少包含：原始 `model_call_id`、稳定错误码、错误范围、可关联 call ID、未执行事实和 Projection 能力诊断。无效项是 `validation_failed`，合法兄弟项是 `not_executed_batch_rejected`，两者不能都写成“参数错误”。身份不可关联时只写 batch-level diagnostic，不创建正常 Tool Results。

### 6.2 两类反馈投影

| 可关联性 | 下一次请求的推荐投影 | 必要验证 |
|---|---|---|
| response 和 call IDs 均可合法重放 | Provider 允许的 assistant call 历史 + 每个 ID 的关联反馈：无效项说明校验错误，其他项说明整批拒绝未执行 | 原始 arguments 是否允许原样回传、每个 call 是否必须有 output、角色顺序、空数组规则 |
| ID 缺失/冲突、响应截断或 wire 结构无法合法重放 | 事件日志仍保存原响应；模型投影不伪造 calls/results，追加明确标记的 synthetic protocol diagnostic，说明没有 Tool 执行 | 不污染用户输入身份；不把诊断伪装成模型回答；Provider 接受该合法请求 |

关键不是选择 tool role 还是 user role，而是 **每个 Provider 都用真实 wire 样例证明合法**。OpenAI converter 对 call/output 配对、空 Tool 数组、reasoning/provider_data 的处理可作为参照，但不能替代 nextloop 自己的目标 Provider 测试。[converter 依据][o-converter]

### 6.3 Correction 条件与失败关闭

只有同时满足以下条件才自动修正：

- rejection 必需事实已提交；
- 当前 Run/lease 有效，未收到 `/stop`；
- 本响应尚未用完 correction allowance；
- 能生成 Provider 合法且 preflight 通过的新请求；
- 没有要求禁止继续的 unknown 执行或资源状态。

任一条件不满足：不调用模型、不执行 Tool，进入统一 finalization 并报告实际原因。反馈构造失败或下一次必需提交失败同样失败关闭。不得把原请求当成“没有输出”做 exact-byte retry，也不得修改已提交的 Provider-visible 前缀。

PydanticAI 的结构化 retry feedback 和校验/执行分界可作为参考；其“坏项反馈、合法兄弟项仍执行”与整批拒绝冲突，不能照搬。[PydanticAI 校验与执行][p-after-validation]

## 7. 建议：append-only Event 与恢复实现依据

### 7.1 事件和事务组

沿用公共 envelope 的 `schema_version/session_id/turn_id/run_id/model_call_id/attempt/seq`。以下是**逻辑事务组建议**，事件名不是已发布 Schema。

| 事务组 | 最少需要原子保存的内容 | 后续动作许可 |
|---|---|---|
| input/final 决策 | input acceptance 顺序，或 final candidate 的 `final_message_id` 决策 | 先接纳的 input 留在当前 Turn；先 final commit 的输入归下一 Turn |
| response settlement | 完整或 partial response、usage、Provider 协议事实 | 完整响应才可分类；partial 不执行 Tool |
| batch rejection | 校验错误、整批零执行、逐项或 batch-level 反馈事实 | 仅可进入 correction/finalization |
| batch admission | 整批通过事实、全部 Tool intents 与原始顺序 | 才可开始第一项 Tool |
| execution start | call ID、intent seq、Run/lease fence、执行句柄/资源事实 | 才可调用有副作用的 Tool body |
| tool result | 真实/ordinary error/cancelled/unknown、partial output、assets、资源限制、intent/started 引用 | 提交后才可 dispatch 下一项或构建下一模型请求 |
| batch close | unknown 关批原因、未启动项 not-executed、保留结果 | 旧批永不自动恢复 |
| run/finalization | 结束诊断、未完成调用处置、资源 owner/restrictions、delivery handoff | 安全释放 writer；未解决资源仍受限制 |
| delivery | `final_message_id`、投递回执或 `delivery_unknown` | 只重试 delivery，不重跑模型/Tool |

EventStore 必须以 expected seq、事务、幂等键、lease/fence 强制执行边界；Loop 决定写哪些事实，Store 决定原子性。preview deltas 不进入权威日志。DSH 的连续 seq、恢复时同规则校验和 log-only/surface 分离是直接结构参考。[DSH 恢复校验][dsh-seed]、[surface 实现][dsh-surface]

### 7.2 Projection 与损坏历史

Projection 应是纯函数：从最新有效 Compaction 边界及其后事件构建 Conversation，无 Compaction 时读全量；在线增量 fold 与启动全量 fold 必须相同。恢复至少检查：

- `seq` 连续，Event envelope/payload 版本可识别；
- 引用只指向已有、合法的 intent/response/compaction；
- 未知必需事件、非法 replacement 或冲突身份失败关闭；
- 只有显式声明可忽略的未知事件才可保留但不影响 Conversation；
- 无效模型 wire 可以在 Trajectory 中存在，但只有合法投影进入下一请求。

“失败关闭”意味着不把该 Session 当作已恢复并继续执行；应保留可诊断错误和原始日志，而不是删除尾部或另建 Snapshot 猜测状态。

### 7.3 启动恢复算法

1. 找到无 closing facts 的 Runs；先确认真实执行已死亡，取得恢复写权和 fence，不能仅信旧 `running` 字段。
2. 校验并重放事件，重建 Conversation、未决 input、final delivery 和资源限制；不重放模型或 Tool 副作用。
3. 对死 Run 幂等追加 `run_interrupted` 及必要 closer。重复恢复看到 closer 后不得再次生成另一组结果。
4. response 已提交但无 final 决策：保留为历史，**不可投递**；不自动继续旧执行。
5. Tool intent 已提交但无 started：标为“未确认开始/未决”，**不自动执行**；若存在可信 started 而无 result，则标 unknown。二者都不因恢复文案而变为可安全重试。
6. unknown 执行的资源限制跨 Run、Turn、Session 和进程恢复保持，直到有验证过的 termination/isolation 与 release 事实。
7. final 已提交但未投递：按 `final_message_id` 仅恢复投递；外部成功但 receipt 未提交则 `delivery_unknown`，使用幂等或查询，不重跑模型。
8. stale WaitingRequest 失效，旧 Run 回调受 fence 限制；新 Turn 只能在既有 writer/delivery 交接规则后启动。

DSH 的 closer 可借鉴“扫描 committed prefix、补关联结果、追加 interrupted 边界、幂等收尾”的形状；其 `TOOL_NOT_STARTED` 文案允许按语义重试，不能覆盖 nextloop 的更保守恢复规则。[DSH closer][dsh-repair]

## 8. 崩溃恢复测试清单

以下应同时有 deterministic fake 测试和真实 Store 的进程终止测试；内存 mock 只能证明分支，不能证明 durability。每个用例都检查事件顺序、call/result 身份、projection、writer/lease、实际副作用次数和用户可见结果。

### A. EventStore / projection

- [ ] 重复幂等键返回原提交，不产生第二事件；expected-seq 冲突、部分事务、磁盘错误均不暴露半个事务。
- [ ] stale lease/fence 的 append 被拒绝；旧 Run 的迟到 result/delivery callback 不能改新 Run。
- [ ] seq 空洞、重复、非法 payload、未知必需事件失败关闭；显式 ignorable 未知事件按规则保留。
- [ ] 在线增量 projection 与重启全量 projection 相同；compaction 前历史仍在日志中，未提交 compaction 无效果。
- [ ] preview-only delta 崩溃后不成为 authoritative response；大结果 asset 先于引用，缺 asset 时引用不被当作完整可用结果。

### B. 模型响应与修正窗口

- [ ] input accepted 后、Run start 前崩溃：输入不丢，不重复接纳。
- [ ] Run/model call/attempt start 后无输出崩溃：旧 Run interrupted，不自动续跑。
- [ ] 有 partial output、response 未提交：不投递、不执行 Tool、不 exact-byte 盲重试。
- [ ] 完整 response 已提交、batch validation 未提交：恢复只保留响应事实，不执行其 calls。
- [ ] batch rejection 已提交、correction 未开始：不自动执行或无限修正；后续合法 Turn 能看到正确诊断投影。
- [ ] correction 已发出但未提交响应：原 rejection 仍在；不重复执行任何被拒绝调用。
- [ ] 空响应、空 Tool 数组错配、截断尾部、混合响应分别命中第 5 节，不发生前缀执行。
- [ ] final candidate 已提交但 final 决策未提交：恢复后不可直接交付该 candidate。
- [ ] steer 与 final commit 竞态：每条 accepted input 恰好归属当前 Turn 或下一 Turn；stop 后 pending steer 保留。

### C. Tool 执行与 unknown 窗口

- [ ] batch admission/intents 已提交、首次 dispatch 前崩溃：不自动执行；无法证明未开始时不得标成安全 not-started。
- [ ] 有 started、Tool body 是否产生副作用未知、无 result：保留 unknown；新 Turn 只能经 Runtime 准入核查，不能盲重放。
- [ ] Tool 已完成副作用、result 未提交：恢复不重复副作用；可用外部证据由后续新动作收集。
- [ ] result 已提交、下一 call 未开始：不重复前项；旧批不能由恢复调度器自动续跑。
- [ ] result commit 失败：下一 Tool 和下一模型请求都未发生；资源/结果责任仍可诊断。
- [ ] A ordinary error、B independent success：两者身份与结果均保留，A 未自动重试。
- [ ] unknown A、未启动 B/C：batch closed，B/C 均有 correlated not-executed，旧批不恢复。
- [ ] 清理期间真实结果到达：真实结果优先于合成 timeout；partial output/assets 不丢。
- [ ] 不合作执行崩溃或失联：writer 可收尾，但资源限制不释放；其他 Session 也不能复用冲突资源。
- [ ] `/stop` 与 timeout/result commit 并发：stop 优先，不触发 same-Turn inspection；真实结果和 unknown 如实保留。

### D. finalization / delivery / 重复恢复

- [ ] final commit 后、delivery 前崩溃：同一 `final_message_id` 仅重试投递，模型和 Tool 调用次数不增加。
- [ ] 外部投递成功、receipt 未提交：`delivery_unknown`，通过幂等/lookup 处理，不重新生成答案。
- [ ] durable DeliveryOwner 接管前崩溃：writer 不提前释放；接管后崩溃也不允许下一 Turn 越过旧 final。
- [ ] finalization 在资源清理、结果写入、run close 各点崩溃：已完成 checkpoint 不重复产生副作用，未解决限制继续有效。
- [ ] 同一日志连续恢复两次、两个恢复者竞争：只有一组合法 closer；不出现重复 Tool Results、Turn closers 或 delivery owner。
- [ ] 旧进程实际仍活着：不得仅因启动扫描判 interrupted；先解决 owner/fence，避免双 writer。
- [ ] stale WaitingRequest、late answer、late Tool callback 均不能授权新动作或清除资源限制。

Hermes 的 cleanup/final-owner 回归可用于场景设计，DSH 的 recovery closer 可用于事件形状设计；只有上述 nextloop 自有故障注入才能证明 L12/L13/L15。[Hermes 测试依据][h-race-test]、[DSH 修复依据][dsh-closers]

## 9. 可直接借鉴与不适用清单

| 来源 | 可借鉴的机制（不是复制依赖） | 不适用 / 不得照搬 |
|---|---|---|
| DSH | append-only 唯一日志；连续 seq；surface/projection 分离；response→intent→result 顺序；结束前后检查；纯 closer 恢复 | Cordis/插件生命周期；Turn/Step 身份模型；逐项坏参数诊断；next-step/followup 多入口；把其 not-started 文案当重试授权 |
| Hermes | 文本出口独立分类；有界 correction 思路；统一 finalizer；逐清理步骤故障注入；final/steer/late-drain 竞态场景 | 内存 buffer 当 durable inbox；leftover 规则替代 final 前串行化；hard stop 清 steer；必需持久化失败仍当可交付成功；业务 stop-gate 插件 |
| PydanticAI | schema+validator 同源；校验/执行分界；重复 ID 预检；结构化 retry feedback；TestModel | 无效项不妨碍合法兄弟项；并行/sequential 配置代替提交协议；durable execution 自动 replay/retry |
| OpenAI Agents SDK | Model/ModelProvider 小缝；call ID 关联约束；wire converter；reasoning opaque 数据保留；ScriptedModel 精确脚本与调用断言 | Runner 作为 Loop；可变消息 Session 当事件权威；RunState 代替事件恢复；handoff/guardrail 进入核心 |

因此实现上只需自研一个小型 driver、明确的 Tool batch flow、EventStore 事务/projection/recovery 和统一 finalizer。底层 JSON Schema 校验器或 HTTP client 是否复用可另行做窄接口评估；本研究不建议引入上述任一项目的执行框架。

## 10. 建议的最小关闭产物

1. 将第 5 节变成 Provider-neutral `ResponseFacts → DriverAction` 决策表测试，覆盖每个分支的 commit owner 与禁止事项。
2. 为每个目标 Provider 保存合法/非法响应和 correction 的 wire fixtures，验证没有伪造 ID、重写旧前缀或丢失 unknown 字段。
3. 固定事件 schema、事务组、幂等键和 lease/fence 规则；给出在线/冷启动 projection 等价测试。
4. 用真实 Store 执行第 8 节每个 commit-window 杀进程测试，再补模型场景验证依赖动作确实跨模型调用。
5. 明确 correction 数值预算、intent/started 事件是否需要，以及资源 restriction 的持久化和释放协议；这些是 remaining implementation contracts，不在本文假装已有实现。

[dsh-session]: https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/core/session/src/index.ts#L1-L35
[dsh-seed]: https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/core/session/src/index.ts#L640-L655
[dsh-derive]: https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/core/session/src/index.ts#L998-L1018
[dsh-surface]: https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/core/session/src/surface.ts
[dsh-event]: https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/core/session/src/types.ts#L282-L325
[dsh-ending]: https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/core/agent-loop/src/agent.ts#L394-L432
[dsh-input]: https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/core/agent-loop/src/agent.ts#L170-L189
[dsh-response]: https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/core/agent-loop/src/agent.ts#L566-L603
[dsh-intent]: https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/core/tools/src/batch.ts#L236-L269
[dsh-repair]: https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/core/session/src/repair.ts#L14-L41
[dsh-closers]: https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/core/session/src/repair.ts#L43-L97
[dsh-parse]: https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/core/tools/src/batch.ts#L50-L105
[h-driver]: https://github.com/NousResearch/hermes-agent/blob/ca4360692055bdb4df3a695c164646e528891da8/agent/conversation_loop.py#L1745-L1799
[h-text-exit]: https://github.com/NousResearch/hermes-agent/blob/ca4360692055bdb4df3a695c164646e528891da8/agent/turn_final_response.py#L179-L420
[h-reasoning]: https://github.com/NousResearch/hermes-agent/blob/ca4360692055bdb4df3a695c164646e528891da8/agent/turn_final_response.py#L137-L178
[h-steer]: https://github.com/NousResearch/hermes-agent/blob/ca4360692055bdb4df3a695c164646e528891da8/agent/interrupt_control.py#L310-L321
[h-leftover]: https://github.com/NousResearch/hermes-agent/blob/ca4360692055bdb4df3a695c164646e528891da8/agent/turn_finalizer.py#L841-L854
[h-finalizer]: https://github.com/NousResearch/hermes-agent/blob/ca4360692055bdb4df3a695c164646e528891da8/agent/turn_finalizer.py#L697-L736
[h-cleanup-test]: https://github.com/NousResearch/hermes-agent/blob/ca4360692055bdb4df3a695c164646e528891da8/tests/agent/test_turn_finalizer_cleanup_guard.py#L148-L180
[h-race-test]: https://github.com/NousResearch/hermes-agent/blob/ca4360692055bdb4df3a695c164646e528891da8/tests/gateway/test_pending_drain_race.py#L73-L200
[h-clear]: https://github.com/NousResearch/hermes-agent/blob/ca4360692055bdb4df3a695c164646e528891da8/agent/interrupt_control.py#L278-L308
[p-schema]: https://github.com/pydantic/pydantic-ai/blob/4ee84920d36d5f71c16c08fed320a935c951f2bb/pydantic_ai_slim/pydantic_ai/_function_schema.py#L98-L214
[p-validate]: https://github.com/pydantic/pydantic-ai/blob/4ee84920d36d5f71c16c08fed320a935c951f2bb/pydantic_ai_slim/pydantic_ai/tool_manager.py#L928-L1002
[p-duplicate]: https://github.com/pydantic/pydantic-ai/blob/4ee84920d36d5f71c16c08fed320a935c951f2bb/pydantic_ai_slim/pydantic_ai/_tool_execution.py#L673-L706
[p-after-validation]: https://github.com/pydantic/pydantic-ai/blob/4ee84920d36d5f71c16c08fed320a935c951f2bb/pydantic_ai_slim/pydantic_ai/_tool_execution.py#L724-L745
[p-execution]: https://github.com/pydantic/pydantic-ai/blob/4ee84920d36d5f71c16c08fed320a935c951f2bb/pydantic_ai_slim/pydantic_ai/_tool_execution.py#L1254-L1326
[p-test-model]: https://github.com/pydantic/pydantic-ai/blob/4ee84920d36d5f71c16c08fed320a935c951f2bb/pydantic_ai_slim/pydantic_ai/models/test.py#L55-L175
[p-persistence]: https://github.com/pydantic/pydantic-ai/blob/4ee84920d36d5f71c16c08fed320a935c951f2bb/docs/persistence.md#L13-L23
[o-model]: https://github.com/openai/openai-agents-python/blob/d4f07df2915ddb029d7076595bb7d4ce89322308/src/agents/models/interface.py#L38-L138
[o-schema]: https://github.com/openai/openai-agents-python/blob/d4f07df2915ddb029d7076595bb7d4ce89322308/src/agents/function_schema.py
[o-converter]: https://github.com/openai/openai-agents-python/blob/d4f07df2915ddb029d7076595bb7d4ce89322308/src/agents/models/chatcmpl_converter.py
[o-scripted]: https://github.com/openai/openai-agents-python/blob/d4f07df2915ddb029d7076595bb7d4ce89322308/src/agents/testing/model.py
[o-runner]: https://github.com/openai/openai-agents-python/blob/d4f07df2915ddb029d7076595bb7d4ce89322308/src/agents/run.py#L1996-L2015
[o-final]: https://github.com/openai/openai-agents-python/blob/d4f07df2915ddb029d7076595bb7d4ce89322308/src/agents/run.py#L2102-L2119
[o-session]: https://github.com/openai/openai-agents-python/blob/d4f07df2915ddb029d7076595bb7d4ce89322308/src/agents/memory/session.py#L69-L110
