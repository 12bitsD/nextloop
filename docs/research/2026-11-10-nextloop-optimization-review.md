# nextloop 优化研究：Feature、扩展性、代码质量与性能

日期：2026-11-10
基线：`e298afeeae782eb06bdad94bfe4afabb5be97c07`（`0.4.0rc1`）
范围：只读源码审查、现有测试与静态检查、临时微基准、官方文档与第一手资料。本文不包含产品代码改动。

## 结论

nextloop 不需要更换核心架构。Session 串行、EventStore fencing、append-only facts、ToolOutcome 分类、先提交 final 再 Delivery 的边界应保留。

后续顺序应是：

1. 修复已复现的正确性缺陷，补齐失败路径测试。
2. 用构造注入打开 Harness 扩展，并增加硬执行预算。
3. 在 parity 测试约束下实现增量 Context Projection，消除逐 Step 全量重建。
4. 按真实使用门槛选择 Tool Result spill、流式 preview、MCP、Provider fallback 等能力。

不建议引入通用插件管理器、第二套 EventBus、Router/多 Agent 框架、分布式任务队列或自动副作用重放。

## 1. P0：先修正确性，不做新 Feature

现有基线验证：`140 passed`，ruff 通过，strict mypy 对 27 个源码文件无问题。以下缺陷均由独立最小脚本复现，说明当前测试主要遗漏失败与竞态路径。

| 缺陷 | 证据与影响 | 最小修复与验收 |
|---|---|---|
| 并行 Tool 跨 chunk 截断重复结算 | `src/nextloop/execute/batch.py:102-124`。首块出现 Cancelled/Uncertain 后，剩余调用被追加 Skipped，但循环未退出，后续 chunk 再追加一次。6 个调用、`max_parallel=2` 可产生 10 个结果。 | 统一只结算一次剩余调用；覆盖多 chunk 的 Cancelled、UncertainSettled、UncertainHanging，断言 outcome 与持久化结果的数量、ID、顺序均与输入一一对应。 |
| Recovery 的 call_id 配对作用域错误 | `src/nextloop/events/repair.py:57-92` 按整个 Turn 的 `call_id` 配对，而 `src/nextloop/execute/tools.py:40-47` 只保证单批唯一。跨 Step 重用 ID 时，旧结果会覆盖新调用的未决状态。 | 以 `(step, call_id)` 配对 proposal、TOOL_CALL、TOOL_RESULT；覆盖第二 Step 未 dispatch 与已 dispatch 无结果两种恢复。 |
| final 前取消被记录为内部 FAILED | `src/nextloop/driver.py:329-336,393-394`。`_CancelFinalization` 在内层 try 之前抛出，专门捕获不可达，最终进入通用 Exception。 | 在正确的状态机边界处理取消；用 gated Store 精确安排 assistant append 后、final commit 前取消，断言 CANCELLED、无 FINAL_MESSAGE、无内部异常文本。 |
| 已拒绝批次在恢复时生成虚假 ToolResult | `src/nextloop/events/repair.py:43-92` 未识别 `TOOL_BATCH_REJECTED(executed=False)`，会把协议拒绝且未执行的 proposal 当成待恢复调用。 | 先收集 rejected step，recovery 只关闭 Step/Turn，不制造 ToolResult；补 crash recovery 回归。 |
| 同步 Python Tool 的 timeout 不生效 | `src/nextloop/execute/tools.py:107-129` 先同步调用 handler，仅对 awaitable 使用 timeout/cancel 包装。同步阻塞还会卡住事件循环。 | 首版建议明确“同步 handler 不支持 timeout”，注册时 fail-fast；保留无 timeout 的简单同步 Tool。不要静默忽略配置，也不要为首版引入线程副作用语义。若未来支持线程执行，需另行定义 timeout 后外部效果未知、取消与资源锁规则。 |

同阶段可做的低价值噪音清理仅限：`Harness` 对未知 protocol 显式报错，`_loop_config` 用 `dataclasses.replace` 避免新增字段漏拷。不要借修 bug 重写 Driver。

## 2. Feature：按内核价值排序

### P0：硬执行预算

`TurnDriver.run` 当前没有硬 Step 上限（`src/nextloop/driver.py:94-100`）；`pacing_step_interval` 只是提示模型收敛，不阻止下一次调用（`src/nextloop/contracts.py:408-419`）。

建议为 LoopConfig 增加可选限制：

- `max_steps`、`max_model_calls`、`max_tool_calls`。
- input/output/total token 限额，usage 以 Provider 实报为准。
- `turn_deadline_seconds`，使用 monotonic clock。

语义要求：Step/Tool 在下一次副作用前检查；token 只能在响应返回后阻止下一次调用，不能承诺本次绝不超支；deadline 复用取消与 uncertain Tool 语义；恢复不能重置预算。超限必须有明确事件与 Turn 终态。

参考：[OpenAI Agents SDK max_turns](https://openai.github.io/openai-agents-python/running_agents/)、[PydanticAI UsageLimits](https://ai.pydantic.dev/api/usage/)。

### P1：文本 Tool Result spill

当前完整 Tool 结果同时进入 durable outcome 与模型 Message（`src/nextloop/events/outcome.py:91-128`），之后每个 Step 都可能重复进入上下文。大日志会同时放大 token、投影与 compaction 成本。

首版建议复用 append-only log 保存全文，模型只接收有界 head/tail preview、总字节数和 `(session_id, tool_result_seq)`；通过只读 Tool 按 offset/limit 分段读取。只有 durable result 已提交，preview 才能对模型可见；preview 不得冒充完整结果。

参考固定源码版本：[DSH Spill Storage](https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/docs/subsystems/spill.md)。

### P1（有交互需求时）：非权威流式 preview

Provider 已消费 SSE delta，但公共模型接口只返回完整响应（`src/nextloop/contracts.py:511-515`）。可增加 provider-neutral preview delta 与 committed semantic events，而不是把权威 Event 改成 token 流。

Preview 不写入权威 Trajectory、不触发 Tool、不作为 final delivery；slow consumer 不得阻塞执行；最后一个 delta 不等于 committed final。断线后仍从 EventStore seq 重同步。

参考：[OpenAI Agents SDK Streaming](https://openai.github.io/openai-agents-python/streaming/)。

### P1（有生态接入需求时）：MCP ToolRuntime adapter

MCP 应只实现为 Composite ToolRuntime 的 adapter，不进入 Loop 控制流。首版可只做 stdio，覆盖分页 catalog、命名空间、schema/content 映射、取消、超时和传输中断分类。`isError` 是普通 Tool execution error；传输中断、超时和崩溃必须映射 uncertain，禁止自动重放。

参考：[MCP Tools specification](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)。

### P2：达到明确门槛后再做

- **Provider fallback**：静态有序 chain，只在无 partial output 且错误类别允许时切换；不做 Router。参考 [PydanticAI FallbackModel](https://ai.pydantic.dev/models/overview/)。
- **渐进工具发现**：工具 schema 已显著消耗上下文或影响选择质量时，再做 catalog/search/inspect；需同时测 token、任务成功率和 cache 命中。参考 [MCP Client Best Practices](https://modelcontextprotocol.io/docs/develop/clients/client-best-practices)。
- **Typed final output**：确有程序化消费需求时增加 schema-constrained output；校验失败不得 final commit/delivery。
- **持久化工具审批与显式 resume**：只有向不可信调用方开放危险副作用时才进入核心。它需要审批事件、参数/策略指纹、等待终态、writer fencing、显式 resume 与并发故障矩阵，是独立里程碑，不应和轻量 Tool 权限混做。UI、reviewer 认证仍在 Bot。参考 [OpenAI Agents SDK Human-in-the-loop](https://openai.github.io/openai-agents-python/human_in_the_loop/)。

## 3. 架构扩展性：保留核心，打开 Harness

现有四个适配缝已经足够小：EventStore、ModelProvider、ToolRuntime、DeliveryOwner（`src/nextloop/contracts.py:502-547`）。问题主要是 Harness 没有提供同等注入能力：

- Provider 工厂固定 OpenAI/Anthropic，且强制 `api_key_env`（`src/nextloop/harness.py:186-208,284-293`）。
- Delivery 无条件使用 MemoryDeliveryOwner（`src/nextloop/harness.py:83-107`）。
- Composite 只完整管理 StdioDriver 的 start/close，其他 runtime 没有统一可选生命周期（`src/nextloop/execute/composite.py:74-93,124-131`）。
- Exporter 缺少可选 flush/close，持有外部连接的 exporter 无法统一收尾（`src/nextloop/adapters/observing.py:152-197`）。

建议只为 Harness 增加可选 `provider`、`store`、`delivery`、`tools/runtime` 构造注入；缺省路径不变。依赖所有权遵循“谁创建谁关闭”，调用方注入对象默认由调用方关闭。需要生命周期时检测可选 `start/flush/close` 能力，不扩大所有 Protocol。

Store 扩展必须有 contract suite，而不只是类型签名：原子批次 append、expected_seq/event_id 幂等、writer_epoch fencing、有序读取、surface/recovery 语义、至少一次 Delivery。读取未知 schema_version 时应 fail-fast 或显式迁移。

暂缓项：

- SQLite 使用同步 sqlite3，确实可能阻塞 async 宿主（`src/nextloop/adapters/sqlite.py:71-189`），但当前基准未证明它是吞吐瓶颈。只有明确 heartbeat/P99 延迟目标后，才把完整事务/查询移入受控线程；不能把单条 SQL 拆到不同线程，也不能降低 WAL/synchronous 级别。
- Model attempt 当前批量提交（`src/nextloop/execute/model.py:48-124`），实时 Trace 需改为逐 attempt 提交才有真实时性；应与流式 preview 一起决定，避免两套实时通道。
- `_actors` 与 `_resource_locks` 按 Session 保留（`src/nextloop/loop.py:18-22`）。长生命周期大量 Session 时才设计休眠清理，且不能清除未决资源锁。

## 4. 性能：实测热点与优化门槛

环境：macOS arm64，Python 3.11.18，SQLite 3.53.1，FakeProvider、短消息、no-op Tool、本地临时文件。每项多次运行取中位数；不代表真实模型网络、生产 payload 或 P95/P99。

### 4.1 主热点：逐 Step 全量 Context 重建

每个 Step 都执行 `load_surface + project_events`（`src/nextloop/driver.py:94-109`）。Memory 会筛选并 deepcopy 全部事件（`src/nextloop/adapters/memory.py:63-78`）；SQLite 会读取并 JSON decode surface（`src/nextloop/adapters/sqlite.py:166-181,235-250`）；随后重新投影 Message 并 canonicalize 请求。

| Steps | Memory 中位耗时 | SQLite 中位耗时 |
|---:|---:|---:|
| 1 | 0.664 ms | 2.022 ms |
| 10 | 4.726 ms | 11.644 ms |
| 50 | 41.891 ms | 60.344 ms |
| 100 | 141.194 ms | 150.111 ms |
| 200 | 495.751 ms | 408.817 ms |
| 400 | 1883.033 ms | 1302.332 ms |

100-step cProfile 中，Memory 的主要热点是 `deepcopy`；SQLite 则包括 append、surface 读取、JSON decode 和 projection。不同 Store 的相对常数成本不能外推为普遍优劣，但规模增长明确超线性。

建议实现 append 驱动的增量投影：surface append 时增量构造消息和请求状态，compaction epoch 时显式重建。优化前必须建立旧/新实现 parity：

- 每步 CandidateRequest、canonical bytes、compaction reason 与 WorkingContext 全等。
- 覆盖 steer、protocol correction、rejected batch、provider_data、Tool schema 顺序、compaction 后继续执行及 recovery 后新 Turn。
- 防止调用方 mutation 污染缓存。
- 同一基准 100/400 Step 中位耗时均降低至少 50%；完整 pytest、mypy、ruff 通过。

若 parity 无法证明，不落地复杂缓存；先只做 Memory 不可变快照等小改并单独测量。

### 4.2 次热点：历史查询全量回放

`_query_history` 先 `load_recovery` 全量事件，再投影，最后按 Turn 过滤（`src/nextloop/adapters/observing.py:224-236`）。Memory 中 1000 个已完成 Turn（2000 events）只查询最后一个 Turn，中位仍为 11.570ms。

有历史 API 延迟要求时，先按 Turn 范围读取/过滤，再投影；目标为该场景降低至少 80%，全范围 history 与旧实现完全一致。不能简单 SQL LIMIT 破坏同 Turn 顺序或 recovery 边界。

### 4.3 明确不优化

- 64 个 async Tool、每个 10ms、max_parallel=4，总耗时 184.127ms，接近理论 16 批，未发现调度热点。
- SQLite 单事务 append：1 event 0.096ms、10 events 0.133ms、100 events 0.668ms；不为微基准合并 Tool 事件或降低 durability。
- FakeProvider 下 ModelCaller 约 0.095ms/次；真实 Provider 网络未知，不优化等待框架。
- 正常 recovery 仅一个开放 Turn 时约 0.222ms；不按病态多开放 Turn 重写。
- Exporter 已是单 worker 有界队列；不增加 worker、不隐藏 drop、不无界扩队。

## 5. 推荐里程碑

### M1：Correctness patch

只修第 1 节五个缺陷及对应回归测试。关闭条件：所有复现脚本转为测试，全量测试与静态检查通过，真实取消/恢复场景通过。

### M2：Embedding and guardrails

完成 Harness 构造注入、依赖所有权、未知配置 fail-fast、硬执行预算。关闭条件：第三方 Provider/Store/Delivery/ToolRuntime 不 monkeypatch 即可运行；预算的副作用前保证与恢复计数经过故障测试。

### M3：Context scaling

先建立 parity 与固定 benchmark，再实现增量 Context Projection；Tool Result spill 可独立分阶段落地。关闭条件：语义全等，性能达到上述阈值，长 Tool 输出请求大小有界。

### M4：按产品触发选择

流式 preview、MCP、Provider fallback、typed output、审批 resume 各自独立立项，不捆绑成“大版本全家桶”。

## 6. 不做清单

- 不把 Bot 渠道、权限、用户/群组、定时任务或长期记忆塞入 Loop。
- 不新增通用插件发现、Service Locator、第二 EventBus 或通用 Hook 控制面。
- 不做 Router、通用多 Agent、DAG/工作流引擎、分布式队列。
- 不自动 replay 已 dispatch 的未知副作用。
- 不为假想需求预建大一统多模态、流式 Provider 抽象或二进制 Asset GC 平台。

## 限制与未知

- 性能实验未覆盖真实模型网络、长输出、大 Tool payload、多进程 writer、stdio worker、生产 exporter 和磁盘争用。
- Feature 成本未作工时承诺；优先级依据当前代码机制与通用 Loop 定位，不代表所有宿主都必须实现。
- 同步 Tool timeout 推荐 fail-fast 是轻量首版决策；若真实用户需要阻塞式 SDK 的硬超时，应另立线程执行契约。
- 本研究未修改产品代码；后续实施需独立 review 与真实场景验收。

## 7. 后续 Feature 设计决策（2026-11-10）

### Tool Result 消费策略

不一刀切 spill。ToolSpec 使用可信的 `PREVIEW | PAGED | FULL` 声明，默认 PREVIEW：小结果完整内联，超过可配置阈值才保留 UTF-8 安全的 head/tail、精确省略字节数与 result_ref；PAGED 返回连续窗口和 next_offset；FULL 禁止通用裁剪，超独立硬上限必须显式失败。`concludes_turn=True` 按 FULL 处理。初始实验参数为 50,000 bytes、40% head / 60% tail，需用真实任务评估。

权威 Event 的 outcome 保留全文，模型 Message 保存对应投影。第一阶段只承诺减少模型 token；只有 surface 不再读取全文后，才承诺降低 Store I/O、JSON decode 或 deepcopy 成本。

### Streaming Preview

采用可选 PreviewSink 与 Provider 可选 `complete_with_preview()`；旧 `complete()` 和非流式第三方 Provider 保持兼容，非流式路径只给 response snapshot，不伪造 token delta。

PreviewEvent 使用 run-local stream_id/seq，包含 text/refusal/tool-call delta、attempt started/ended/resolved 与 reset。preview 不进入 EventStore、不执行部分 Tool arguments、不触发 Delivery；resolved 只在对应 assistant decision 提交后发出。EventStore 仍是唯一事实源。

核心提供有界非阻塞队列；相邻 delta 可合并，溢出通过带最新瞬态 snapshot 的 RESET 恢复，控制事件尽量不丢。关闭 preview 只退订，不取消 Turn；停止执行仍显式调用 `AgentLoop.cancel()`。首版不做多订阅、原始 SSE 暴露、持久 replay 或 Last-Event-ID。

### MCP 与工具发现

MCP 实现为 ToolRuntime adapter，Loop 不感知 MCP。工具发现默认仍使用完整列表；达到实测 schema token 或选择质量门槛后，提供确定性分页 `list_tools` 和精确名称 `inspect_tools`。BM25 `search_tools` 只是可选候选加速，必须证明相对 exact/substring/regex 的收益，不能成为唯一入口。

不增加 `execute`/`call_tool` 元工具。模型知道 Schema 时可直接发起普通 Tool Call；需要时先 list/search/inspect，随后仍走现有 BatchExecutor 的名称、参数 JSON 和 JSON Schema 校验，错误交给现有 correction。静态目录首版不增加 revision 检查；只有实现动态 MCP catalog/listChanged 时才评估 revision 冲突。授权由宿主的 authorized catalog 与执行路径保证，不在核心虚构租户模型。

### Typed output

维持 P2。现有 `Completed(concludes_turn=True)` 是 nextloop 的 terminal Tool 机制：Tool content 直接成为 final，不再调用模型生成答复，可由 Tool 自己完成结构化校验。确有大量程序化 final 消费需求时，再实现 Provider 原生 Schema 约束和 Loop 校验后的 structured final。
