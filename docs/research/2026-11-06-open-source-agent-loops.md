# 开源 AgentLoop 内核评估

> **历史候选调研。**后续已决定直接 follow DSH AgentLoop 主干并保留明确差异，见 [ADR-0002](../adr/0002-dsh-aligned-agent-loop.md)。本文的“reference + self-build”和串行首版建议不再代表当前架构。

核验日期：2026-11-06。Nika 基线：`main@96135332fb4e5b4d9e2621afd63af6803c2a886d`，当时只有设计文档，没有 runtime。本轮只读取固定版本的源码和官方文档，未运行候选项目、模型或测试。“支持”表示源码存在，不表示端到端实测通过。

## 结论

**当时没有候选适合直接作为 Nika 的完整内核。建议自研 driver，参考 DSH、Hermes、Yomi 的局部机制。不要把通用 Agent SDK 的 `Runner.run()` 当成可替换的 Loop 内核。**

Nika 的核心价值在候选通常替调用者做决定的地方：唯一事件事实源、final 与 steer 的顺序、Tool 整批准入、unknown 副作用和统一 finalization。复用普通 ReAct 后再绕过其状态边界，维护成本高于自研小型状态机。

独立仓库方向合理，但应做成协议明确、无渠道和知识产品的 library。先用 Nika 的故障矩阵验证，再发布通用包，不先做 EventBus 或多 Agent 框架。

## 1. 筛选标准

初筛要求：

1. 可作为进程内 library 调用，不依赖专属 HTTP 服务。
2. 能表达单 Agent 的 model → Tool → observation → model 循环。
3. 有可检查的 streaming、Tool、取消或持久化源码，而不只是 API 宣传。
4. 许可证允许继续评估，近期仍有维护活动。
5. Python/Rust 优先，其他语言只在机制特别接近时保留。

真正的验收要求：

- Session/Turn/Run/model_call/attempt 身份明确，单 writer 可控。
- append-only `session_events` 是唯一事实源，Conversation 是 Projection。
- steer 只在安全边界注入；final 前已接纳的 steer 继续当前 Turn，final 后进入下一 Turn。
- Provider retry 不新建 Run；无输出且无新输入时请求字节一致，partial output 不盲重试。
- 任一 Tool 格式、Schema、身份或完整性错误，整批零执行；通过后的普通错误可继续，依赖动作跨模型调用。
- timeout 不证明副作用失败；unknown 结束本批，不自动重放，资源限制可持续。
- 正常、停止和失败都进入同一 finalization；恢复只重建事实和必要交付，不续跑旧指令。
- ContextBuilder、专用 Compaction Prompt、75%/I+O preflight、WorkingContext 和 Tool Catalog 由 Nika 控制。

## 2. 入围候选

活动时间是固定版本默认分支最新 commit 的 committer date。许可证以该版本的 `LICENSE` 为准。

| 候选（固定版本） | 语言 / license | 嵌入方式 | 状态与事件 | 主要不匹配 |
|---|---|---|---|---|
| [DSH](https://github.com/deepseek-ai/deepseek-harness/commit/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159)，2026-11-04 | TypeScript / [MIT](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/LICENSE) | npm 包，Cordis 服务组合 | 单 driver；turn/step；next-step/next-turn inbox；append-only Session、Projection 和 crash closer | 非 Python/Rust；深度绑定 Cordis；Tool 逐项容错，不是整批 Schema 拒绝 |
| [Hermes Agent](https://github.com/NousResearch/hermes-agent/commit/ca4360692055bdb4df3a695c164646e528891da8)，2026-11-06 | Python / [MIT](https://github.com/NousResearch/hermes-agent/blob/ca4360692055bdb4df3a695c164646e528891da8/LICENSE) | `AIAgent` 进程内类，也有 CLI/gateway | 完整 Turn driver、steer、stop、finalizer；SQLite 消息和 replay cleanup | 大型产品内核；final 后 leftover steer 交下一 Turn；unknown timeout 后普通串行批仍可继续 |
| [Yomi](https://github.com/Crescent617/yomi/commit/2193746a4a2db2c17a62c5e66ba66a2c4e25056a)，2026-11-02 | Rust / [AGPL-3.0-only](https://github.com/Crescent617/yomi/blob/2193746a4a2db2c17a62c5e66ba66a2c4e25056a/LICENSE) | `crates/kernel` library，也有 daemon/渠道 | Agent 状态机、mailbox、WindingDown；EventBus 后异步落盘 | AGPL 约束；final 不查 Pending steer；并发 Tool；stop 清 mailbox；retry 未按 partial/steer 准入 |
| [OpenAI Agents SDK](https://github.com/openai/openai-agents-python/commit/d4f07df2915ddb029d7076595bb7d4ce89322308)，2026-11-06 | Python / [MIT](https://github.com/openai/openai-agents-python/blob/d4f07df2915ddb029d7076595bb7d4ce89322308/LICENSE) | `Runner.run` / `run_streamed` | Runner 自有 turn loop、handoff、guardrail；Session 是可 pop/clear 的消息存储；RunState 可恢复部分中断 | 无普通运行中 steer；Session 不是 append-only Event 权威；final、Tool 和恢复协议不同 |
| [LangGraph](https://github.com/langchain-ai/langgraph/commit/5847733bcb186c26b32da5cc822eaf8a89deffeb)，2026-11-06 | Python / [MIT](https://github.com/langchain-ai/langgraph/blob/5847733bcb186c26b32da5cc822eaf8a89deffeb/LICENSE) | 编译图后 `invoke/stream` | 通用 node/superstep/interrupt；checkpoint、channel、pending writes | checkpoint 不是 append-only Event 权威；Nika 仍需自写 Loop、Provider 和副作用规则 |
| [PydanticAI](https://github.com/pydantic/pydantic-ai/commit/4ee84920d36d5f71c16c08fed320a935c951f2bb)，2026-11-06 | Python / [MIT](https://github.com/pydantic/pydantic-ai/blob/4ee84920d36d5f71c16c08fed320a935c951f2bb/LICENSE) | `Agent.run` / graph nodes | 自有 model/tool graph；message history、StepPersistence、可选 durable engine | 校验失败按项反馈，合法兄弟项仍执行；无运行中 steer；durable execution 会重试进行中任务 |

未入围：AutoGen、CrewAI 主要是多 Agent/团队；Agno、Letta 偏完整平台或服务；smolagents 很轻，但没有上述持久执行协议。它们即使能完成普通 ReAct，也不能减少 Nika 最难的工作。这里不评价其产品质量。

## 3. 源码证据与适用边界

### 3.1 DSH：最重要的结构参考，不建议直接 fork

事实：

- `Session` 明确使用 event-sourced、append-only log 和派生 LLM history；`SessionEventMap` 可扩展。[模块边界](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/core/session/src/index.ts#L1-L35)、[事件协议](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/core/session/src/types.ts#L282-L325)
- `steer()` 写 next-step，`followup()` 写 next-turn。Turn 在结束原因产生后，会在 stopping hook 前后各检查一次 next-step；有输入就不退出。它最接近 Nika 的 final 前 Pending steer，但身份模型不同。[输入 API](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/core/agent-loop/src/agent.ts#L170-L189)、[结束检查](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/core/agent-loop/src/agent.ts#L394-L432)
- 每次响应先 append assistant message，再执行 Tool；Tool Intent 在 dispatch 前 append，Result 引用 call seq。[响应与派发](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/core/agent-loop/src/agent.ts#L566-L603)、[intent/result](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/core/tools/src/batch.ts#L236-L269)
- 崩溃修复为已开始但无 Result 的调用生成 `TOOL_OUTCOME_UNKNOWN`，关闭旧 Turn，不自动续跑。[recovery](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/core/session/src/repair.ts#L14-L41)、[追加 closer](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/core/session/src/repair.ts#L43-L97)
- Tool 参数先逐项 `JSON.parse`，坏 JSON 保留为字符串交给 Tool 自己诊断，不是整批 Schema 准入。[参数解析](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/core/tools/src/batch.ts#L50-L105) 取消会等待已启动 body settle，但不合作 body 的资源限制仍需另建。[取消边界](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/core/tools/src/pipeline.ts#L243-L285)

判断：参考 Session/Projection、request header、inbox、crash closer 和测试。直接采用会同时引入 TypeScript、Cordis 和另一套事件/Tool 语义，改造成本过高。

### 3.2 Hermes：steer/finalization 参考，不宜作为依赖内核

事实：

- 原生 loop 根据有无 Tool Calls 选择 Tool round 或文本出口，结束后统一 `finalize_turn`。[driver](https://github.com/NousResearch/hermes-agent/blob/ca4360692055bdb4df3a695c164646e528891da8/agent/conversation_loop.py#L1745-L1799)
- `steer()` 是有锁的内存 buffer，Tool 边界可注入。finalizer 将 final 后剩余的无 ticket steer 作为 `pending_steer` 返回下一 Turn，不继续当前 Turn。[steer](https://github.com/NousResearch/hermes-agent/blob/ca4360692055bdb4df3a695c164646e528891da8/agent/interrupt_control.py#L310-L321)、[leftover](https://github.com/NousResearch/hermes-agent/blob/ca4360692055bdb4df3a695c164646e528891da8/agent/turn_finalizer.py#L841-L854)
- 普通串行 Tool 错误通常形成 Result 后继续；持久化失败会阻止后续派发。[串行执行](https://github.com/NousResearch/hermes-agent/blob/ca4360692055bdb4df3a695c164646e528891da8/agent/tool_executor.py#L1870-L1924)
- 工具不存在时会尝试修名并过滤坏项，合法兄弟项可继续，不是 Nika 的整批拒绝。[名称处理](https://github.com/NousResearch/hermes-agent/blob/ca4360692055bdb4df3a695c164646e528891da8/agent/turn_tool_validation.py#L101-L127)
- timeout 可记录 `effect_disposition=unknown`，普通串行路径仍可执行后续项；所检查 Chat transport 还会剥掉该字段。[timeout](https://github.com/NousResearch/hermes-agent/blob/ca4360692055bdb4df3a695c164646e528891da8/agent/tool_executor.py#L866-L994)、[wire 过滤](https://github.com/NousResearch/hermes-agent/blob/ca4360692055bdb4df3a695c164646e528891da8/agent/transports/chat_completions.py#L37-L45)

判断：借鉴 safe steer 边界、未完成调用的关联结果、finalizer 分项清理和回归场景。不要继承 gateway、subagent、Kanban、hook、名称修复或消息存储。

### 3.3 Yomi：Rust 状态机和清理参考，不宜 fork

事实：

- 正常 `Stop/Repeat` 直接进入 `WindingDown`，没有 Pending steer 检查。[transition](https://github.com/Crescent617/yomi/blob/2193746a4a2db2c17a62c5e66ba66a2c4e25056a/crates/kernel/src/agent/agent.rs#L1574-L1648)
- streaming retry 按错误类别和次数执行；循环不消费 steer，也不以已有 partial output 为准入条件。[retry](https://github.com/Crescent617/yomi/blob/2193746a4a2db2c17a62c5e66ba66a2c4e25056a/crates/kernel/src/agent/agent.rs#L1653-L1714)
- Tool 在获准后并发派发，再按调用收集结果，没有先按完整批次 Schema 拒绝。[派发/收集](https://github.com/Crescent617/yomi/blob/2193746a4a2db2c17a62c5e66ba66a2c4e25056a/crates/kernel/src/agent/agent/tool_exec.rs#L324-L469)
- 取消会 abort 任务并补齐未启动项 Result，保留已完成结果；合成 cancelled 不能证明外部副作用停止。[取消配对](https://github.com/Crescent617/yomi/blob/2193746a4a2db2c17a62c5e66ba66a2c4e25056a/crates/kernel/src/agent/agent/tool_exec.rs#L301-L347)
- 许可是 AGPL-3.0-only。复制或修改其代码形成可复用组件需要单独评估，不能按 MIT 项目处理。[许可声明](https://github.com/Crescent617/yomi/blob/2193746a4a2db2c17a62c5e66ba66a2c4e25056a/README.md#L223)

判断：只借鉴 Rust 状态转换、进程组清理和取消配对测试。不复用 kernel，不继承 EventBus、双 mailbox、并发 Tool 和 stop 清输入。

### 3.4 OpenAI Agents SDK：Provider/测试缝可参考，不复用 Runner

事实：

- Runner 自己推进 `while True`、turn 计数和 `NextStepFinalOutput`，handoff 和 guardrail 都在其协议内。[主循环](https://github.com/openai/openai-agents-python/blob/d4f07df2915ddb029d7076595bb7d4ce89322308/src/agents/run.py#L1996-L2015)、[final 分支](https://github.com/openai/openai-agents-python/blob/d4f07df2915ddb029d7076595bb7d4ce89322308/src/agents/run.py#L2102-L2119)
- `Session` 提供 `get_items/add_items/pop_item/clear_session`，是消息历史，不是不可变 Event log。[Session](https://github.com/openai/openai-agents-python/blob/d4f07df2915ddb029d7076595bb7d4ce89322308/src/agents/memory/session.py#L69-L110)
- `RunState` 能恢复审批和部分 Tool/Session 写入窗口；官方要求相同 backend、session_id 和独占访问，历史尾部不匹配就失败关闭。这不等于 Nika 的事件恢复。[恢复边界](https://github.com/openai/openai-agents-python/blob/d4f07df2915ddb029d7076595bb7d4ce89322308/docs/results.md#L140-L142)
- streaming result 有 `cancel()`，但所查公共运行接口没有普通运行中 steer；`RunState.add_input()` 是 resume 前 staged input，不是 final 前竞态协议。[结果接口](https://github.com/openai/openai-agents-python/blob/d4f07df2915ddb029d7076595bb7d4ce89322308/docs/results.md#L150-L164)

判断：可参考 Model adapter、replay-safe retry 分类和 provider-neutral fake model。不要把 Runner、Session 或 Handoff 当作 Nika 核心。

### 3.5 LangGraph：通用图执行器，不是所需 Loop

Pregel 以 channel、superstep、checkpoint 和 pending writes 保存图状态；恢复是图执行语义。[主执行状态](https://github.com/langchain-ai/langgraph/blob/5847733bcb186c26b32da5cc822eaf8a89deffeb/libs/langgraph/langgraph/pregel/main.py#L1953-L1959)

checkpoint 通过 saver `put` 写入，task writes 另有 `put_writes`。这不是 Nika 唯一 append-only `session_events` + Conversation Projection。[保存路径](https://github.com/langchain-ai/langgraph/blob/5847733bcb186c26b32da5cc822eaf8a89deffeb/libs/langgraph/langgraph/pregel/main.py#L2084-L2105)

判断：采用后只得到通用调度层；AgentLoop、Provider streaming/retry、批次副作用、final commit 和事件恢复仍需自写，还要维护图和 checkpoint 两套概念，收益不足。

### 3.6 PydanticAI：Tool/Provider 局部参考，Loop 行为不匹配

事实：

- 它先校验 function calls 再执行；重复 ID 会在本批执行前失败关闭。[预校验](https://github.com/pydantic/pydantic-ai/blob/4ee84920d36d5f71c16c08fed320a935c951f2bb/pydantic_ai_slim/pydantic_ai/_tool_execution.py#L673-L706)
- 但 `args_valid=False` 会记录为该调用的 retry feedback，合法兄弟项仍执行，不是 Nika 的“一项无效，全批零执行”。[校验后调用](https://github.com/pydantic/pydantic-ai/blob/4ee84920d36d5f71c16c08fed320a935c951f2bb/pydantic_ai_slim/pydantic_ai/_tool_execution.py#L724-L745)
- 支持全局 sequential，也支持默认并行分段。顺序可配置不代表有 intent commit、unknown 关批或资源限制。[执行策略](https://github.com/pydantic/pydantic-ai/blob/4ee84920d36d5f71c16c08fed320a935c951f2bb/pydantic_ai_slim/pydantic_ai/_tool_execution.py#L1254-L1326)
- 官方区分 conversation、StepPersistence 和 durable execution；durable engine 会 replay 已完成 step 并重试进行中 step。这与 Nika 的“恢复事实但不自动重放未决副作用”不同。[持久化分类](https://github.com/pydantic/pydantic-ai/blob/4ee84920d36d5f71c16c08fed320a935c951f2bb/docs/persistence.md#L13-L23)、[边界](https://github.com/pydantic/pydantic-ai/blob/4ee84920d36d5f71c16c08fed320a935c951f2bb/docs/persistence.md#L148-L152)

判断：Provider 适配、Tool Schema 校验和 typed result 是较好的 Python 局部参考。不要复用 graph driver 或 durable replay。

## 4. reuse / fork / reference / self-build

| 路线 | 收益 | 代价与风险 | 当时结论 |
|---|---|---|---|
| 直接 reuse 完整内核 | 初期少写 model/Tool 循环 | 必须接受上游 Session、Turn、Tool 和恢复语义；关键路径仍需 patch 或绕过 | **不推荐，无合格对象** |
| fork DSH/Hermes/Yomi | 已有边界代码和测试 | 继承庞大产品面和上游演进；DSH 是 TS/Cordis；Yomi 是 AGPL；Hermes 与 Nika 的 final/unknown 规则冲突 | **不推荐** |
| 只 reuse Provider/Tool 辅助库 | 少写 adapter、Schema 和协议解析 | 双层 retry、消息转换、partial 和错误分类可能冲突 | **逐接口小范围评估，不绑定 Runner** |
| reference + self-build | 自有提交和恢复协议，核心规模可控 | 必须自己维护 Provider 协议、故障矩阵和 release | **当时推荐** |

自研不等于重写所有代码。可以复用成熟 SDK 的 HTTP client、JSON Schema 校验器和 Provider wire 类型，但不能让这些库另起自动 retry 或 Tool executor，绕过 Nika 的 commit owner。

## 5. 历史独立仓库接口建议

包名可暂称 `agent-loop`，先定语义再定名。首版只提供同步编排的异步 driver，无 HTTP server、Router、多 Agent、渠道或 Wiki。

```text
AgentLoop.run(execution: RunContext) -> RunOutcome
AgentLoop.steer(input) / cancel(cause)   # 经由 Session input protocol，不另建任务队列

EventStore
  append_expected(...)                    # seq、事务、幂等、lease/fence
  transaction(...)                        # input acceptance 与 final commit 的确定顺序
  load_events(session_id, after_seq)
  recover_open_runs()

ModelProvider
  stream(AdmittedRequest, attempt, cancel) -> ProviderStream
  count_tokens(candidate) -> Usage | unavailable
  # 返回 wire/protocol 事实；不自行运行 Loop 或 Tool

ContextBuilder
  build(projection, revisions, pending_input) -> CandidateRequest
  preflight(candidate) -> Admit | CompactRequired | Reject

Compactor
  compact(CompactionRequest) -> CompactionResult

ToolRuntime
  validate_batch(response, catalog) -> AdmittedBatch | BatchRejection
  execute(call, cancellation, deadline) -> ToolExecutionOutcome
  # outcome 分离 result / cancelled / unknown、执行证据、部分输出、资源限制

TrajectoryRecorder / Finalizer / DeliveryOwner
  # 明确每个事件的唯一 commit owner；finalization 是所有出口必经路径
```

`AgentLoop` 拥有状态迁移和安全边界；Store 拥有原子提交和 fence；ToolRuntime 拥有真实执行和资源证据；Provider 不拥有 Nika 的 retry 决策；Delivery 不拥有执行历史。

历史 release 建议：

- Event envelope 含 `schema_version`、Session/Turn/Run、逻辑调用和 attempt 身份、单调 `seq`。业务 payload 可版本化扩展，不能用自由日志代替事实。
- Provider request/response 和 Tool outcome 使用独立 Adapter 版本，不把 SDK 公开类变成内核永久 API。
- Preview stream 与 durable Event 分离，delta 不进入权威 log。
- 每个 release 带 Event migration、兼容范围和故障注入测试。SemVer 与 Event Schema 分开。
- 发布前覆盖 final/steer race、各状态 cancel、整批无效零执行、普通错误续批、unknown 关批、partial retry 禁止、字节一致 retry、每个 commit 窗口杀进程、delivery-only retry 和持续资源限制。
- 当时倾向 Python，理由是 Browser/Computer、Provider SDK 和工具生态的组合成本较低。这是工程推断，不是实测结论。

## 6. 当时建议

1. 自研 Nika driver 和事件提交协议。
2. DSH 作为第一结构参考，重点读 Session/Projection、next-step 结束检查、request header 和 crash closer。
3. Hermes 作为第二生命周期参考，重点读 steer、finalizer、取消和清理测试。
4. PydanticAI/OpenAI Agents SDK 只作 Provider、Schema、fake model 和 replay 分类参考。
5. Yomi 只读 Rust/进程管理，不复制代码；未来若 fork，先处理 AGPL 义务。
6. 不复用 LangGraph 作为 Loop；它增加 checkpoint/graph 概念，却不解决核心协议。
7. 不复用 AutoGen、CrewAI、Agno、Letta 或 smolagents 作为内核；它们不降低最难部分的实现和验证成本。

## 7. 独立仓库与 Nika 的历史分工

| 独立 `agent-loop` 仓库 | `nika` 仓库 |
|---|---|
| Session/Turn/Run/ModelCall/Attempt 身份；单 writer/lease/fence | Feishu 私聊、群、Thread、文档评论入口与 Session 映射 |
| AgentState、单 steer buffer、安全注入与 final commit 顺序 | Feishu 消息幂等、渲染、卡片、投递客户端与渠道 UX |
| append-only Event envelope、Projection/Recovery、事务接口 | Nika 的 SQLite 配置、备份、GC 和 asset 策略；通用 Store 成熟后可下沉 |
| model invocation、attempt/retry 准入、partial 分类 | 具体 Kimi/OpenAI/Anthropic 路由、凭据和模型 profile；通用 Adapter 可下沉 |
| Tool 整批准入、串行派发、unknown 关批、结果关联 | Feishu/Browser/Computer/Wiki/Skills 具体 Tool、授权和 GUI/进程资源 |
| finalization、资源限制接口、delivery-only 恢复 | 实际资源监督、容器、虚拟桌面和渠道投递责任接管 |
| ContextBuilder/Compactor 的通用 candidate、preflight 和提交边界 | Nika 的固定 Prompt、AGENTS.md、WorkingContext 内容规则、75%/I+O 配置、Wiki/Skill 读取 |
| provider-neutral contract tests、假 Provider/Tool/Store、故障矩阵 | Nika 端到端验收、真实 Feishu/Browser/Computer smoke、部署和迁移 |

**切分检验：**独立包换成另一个聊天产品仍应可复用；Nika 换掉 Feishu 入口不应改 Loop。如果独立包必须知道 Wiki、飞书消息 ID 或某个桌面锁才能跑测试，边界就错了。
