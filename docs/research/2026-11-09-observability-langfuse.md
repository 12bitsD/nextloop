# 可选观测层与 Langfuse / OpenTelemetry 接入边界

> **只读技术调研与设计建议，不代表已实现的接口或已选定供应商。**外部事实核验日期：2026-11-09（Asia/Shanghai）；nextloop 基线：`main@4cb3bcccddec7a3f489fdf8f18be4086272e1af5`。本轮只查阅官方文档、官方规范和本地源码，未调用 Langfuse、未安装 SDK、未做端到端投递测试。

下文将 **事实**（官方或本地源码依据）、**推断**（工程判断）和 **建议 / 待决策**（尚未成为产品契约）分开。

## 1. 结论摘要

1. **采用 `ObservingEventStore + ObservationSink` 作为可选装饰层：先完成权威提交，再异步观测。** 观测失败不得改变 `seq`、writer epoch、Provider retry、Tool 执行、Recovery 或 Delivery，也不能把观测导出当成业务事务。
2. **首选提交后统一投影，不在 Provider、Tool、Recovery 中各写一套遥测。** 当前 append-only 日志已有 Turn/Step/Attempt、Tool call/result 和恢复事实，足以生成第一版拓扑。内部插桩以后只用于补充精确 wire 时间或流式指标，不能成为第二事实源。
3. **中性模型负责 nextloop 语义，OTLP Adapter 负责标准映射，Langfuse Adapter 负责平台增强。** `ObservationSink` 不暴露 Langfuse SDK 类型，也不承诺所有后端都有 Prompt 管理、ScoreConfig、annotation queue、media 或 experiment runner。
4. **建议一个 Turn 对应一个 trace，以 `session_id` 聚合多 Turn；一个 Provider attempt 对应一个 generation。** Step 是中间 span，Tool execution 是子 span；input acceptance、batch rejection、recovery 和 delivery 是点事件或生命周期结束事实。retry 保留独立 attempt。
5. **OTLP 是投递通道，不是完整 Langfuse 功能协议。** Score/eval、数据集和托管 Prompt 需要独立能力接口或 Langfuse 原生 SDK/API。离线 eval 优先读取 nextloop 已提交日志，不依赖在线采样后的副本。

## 2. Langfuse 接入方式与数据模型

### 2.1 Python SDK 与 OTLP endpoint（事实）

- 当前主版本是 **Python SDK v5 / JS/TS SDK v6**，要求 Langfuse v4；自托管 Python v5 的最低服务端为 v4.59.0。可通过 `get_client()`、context manager、decorator 或手动 observation 创建嵌套 span/generation，SDK 自动配置 OTel。[SDK 文档][lf-sdk]
- Python/JS SDK 建立在 OTel 上。已有 OTel 系统可优先使用其 SDK 获得属性传播、media、过滤和导出处理；其他语言或 pipeline 可直接导出 OTLP。[OTel 集成][lf-otel]
- 接收端为 `POST {base_url}/api/public/otel/v1/traces`，通用 base endpoint 为 `/api/public/otel`。支持 **OTLP/HTTP JSON 与 protobuf，不支持 gRPC**。认证使用 HTTP Basic：public key 为用户名，secret key 为密码。[OTel endpoint 与认证][lf-otel]
- 直接 OTel 接入要求 `x-langfuse-ingestion-version: 4` 才进入 v4 实时摄取；否则文档称数据可能延迟至多 10 分钟。这是 Langfuse 要求，不是 OTLP 标准字段。[实时摄取要求][lf-otel]
- Langfuse v4 推荐经 OTel 写入 trace/observation。旧 `/api/public/ingestion` 对 trace/observation 已弃用，Cloud 截止日期为 2026-11-16；score helper 使用的 `score-create` 事件在该截止后仍受支持。[Public API 与弃用边界][lf-api]

### 2.2 Trace / Span / Generation / Event（事实）

| 概念 | Langfuse 语义 | nextloop 建议映射（推断） |
|---|---|---|
| Trace | 同一 `trace_id` 下的一组 observations 和共享属性；Langfuse Session 可聚合多个 traces | 一个 Turn 一个 trace；`langfuse.session.id = nextloop session_id` |
| Span / Observation | 有开始、结束、名称、属性和父子关系的操作 | Turn root、Step、Recovery、Delivery 等一般操作 |
| Generation | 面向 LLM 调用的 observation，增加 model、parameters、usage 和 cost 等字段 | 一个物理 Provider attempt；不要把含 retry 的 Step 伪装成一次成功 generation |
| Event | 点时刻动作，而非持续操作 | input accepted、batch rejected、recovery marker；也可作为 OTel span event |
| Tool / Agent 等 | Langfuse 还支持 tool、agent、retriever、evaluator 等类型 | Tool call/result 合成一个 tool span；Turn root 可用 agent 类型，但通用 OTLP 不依赖它 |

来源：[Langfuse core concepts][lf-model]、[SDK 的 OTel 对照][lf-sdk]、[OTel 属性映射][lf-otel]。Langfuse v4 的整体 input/output 属于 root observation，旧 trace-level input/output 已弃用。[SDK 数据模型][lf-sdk]

### 2.3 字段与 eval 边界（事实）

- **可经 OTel：** trace/span 身份和父子关系、起止时间、latency、status/error、metadata，以及 Langfuse 映射的 model、parameters、usage、cost、input/output、completion start time 和 prompt reference。Langfuse 优先解释 `langfuse.*`，也映射多种 `gen_ai.*`。[完整属性映射][lf-otel]
- **需要平台属性：** `langfuse.observation.type`、可过滤的顶层 metadata、Prompt 名称和版本不是通用 OTel 自动保证的语义。普通 OTel 属性进入 `metadata.attributes`，不默认成为可过滤的顶层 metadata。[metadata 规则][lf-otel]
- **Score/eval 是独立对象：** Score 恰好关联 Trace、Observation、Session 或 DatasetRun 之一，支持 numeric/categorical/boolean/text，可带 comment 和 ScoreConfig。创建使用 SDK helper / Public API，读取使用 Scores API v3。OTel trace 属性不会自动创建这些对象。[Score 数据模型][lf-score]、[Score 创建与读取][lf-api]
- **Experiments 也不是通用 trace：** Dataset、DatasetItem、DatasetRun、DatasetRunItem 与 Trace/Observation/Score 有独立关系。官方提供 experiment runner 和 OTel experiment attributes，但没有公共 REST 新建 experiment run 的通用路径。[Experiment 数据模型][lf-experiments]

因此，**不能仅通过 OTLP traces 就承诺 Langfuse Score、ScoreConfig 或 DatasetRun 已正确创建**。需要显式 native capability 或另一个 eval sink。

## 3. OpenTelemetry GenAI 语义约定

### 3.1 稳定性（事实）

GenAI 语义约定已从主 `semantic-conventions` 仓库迁移到 `open-telemetry/semantic-conventions-genai`；旧 OTel 文档页标为 moved/no longer maintained。[迁移说明][otel-moved]

新仓库的 general GenAI spans、client inference、agent spans 和 execute-tool 文档均标为 **Development**，不是 Stable。[GenAI spans][otel-genai]、[client inference][otel-inference]、[agent spans][otel-agent]、[execute tool][otel-tool]

可以使用这些字段改善互操作，但不能把字段名、retry 聚合方式或内容模型当成冻结标准。映射应集中在 Adapter 中并做契约测试，不把 `gen_ai.*` 扩散进 nextloop 内核。

### 3.2 Provider 与 Tool（事实与推断）

- Client inference 适用于 OpenAI Chat Completions、Anthropic Messages 及其流式变体，定义 provider、request/response model、finish reasons、usage、input/output 和 time-to-first-chunk 等字段。[client inference][otel-inference]
- Execute-tool 建议应用手动插桩，定义 `execute_tool {tool name}`、INTERNAL kind、call id、arguments/result 和 error type。[execute tool][otel-tool]
- 当前 GenAI 总则说明 span 表示 caller 看到的**逻辑操作**，自动 transient retry 通常由一个 span 覆盖。[retry 规范][otel-genai] **推断：**nextloop 可用 Step/model-call 父 span 表示逻辑操作，再为每个物理 attempt 建 generation 子 span；若只保留一个标准 inference span，也必须把各 attempt 作为事件或子 span 另存，不能丢失 retry 历史。
- `UncertainHanging` 表示副作用未知，不等于普通失败或成功。**建议：**status/error 之外保留 `nextloop.outcome.kind` 等自有属性，不用单一 OTel `error.type` 吞掉未知语义。

## 4. 导出生命周期、失败、采样与敏感内容

### 4.1 Batching / flush / close（事实）

- Langfuse SDK 在后台批量发送，不在 observation 创建时同步写入；短生命周期进程退出前必须 `flush()`。[Langfuse 后台处理][lf-model]
- OTel batching processor 按定时器、批量阈值或 `ForceFlush` 导出已结束 spans；队列有界，满后会丢 spans。规范默认 queue 2048、delay 5 秒、export timeout 30 秒、batch 512，这些不是 Langfuse 服务限制。[OTel batching][otel-sdk]
- `ForceFlush` / `Shutdown` 应报告成功、失败或超时，并在有界时间内完成或中止；Shutdown 只调用一次并下传至 processors。[OTel 生命周期][otel-sdk]

**建议：**`ObservationSink` 至少区分 `emit`、`flush(timeout)`、`close(timeout)`。业务 Turn 正常结束不等待网络导出，应用或 CLI 最终退出才 flush/close。flush 失败进入本地诊断，不回滚已提交 Turn。

### 4.2 失败与重复（事实）

- OTLP/HTTP 对 429、502、503、504 建议重试，其他未列出的 4xx/5xx 不得重试；有 `Retry-After` 时遵守，否则指数退避。partial success 不应整批重试，永久坏数据应丢弃并计数。[OTLP 失败规则][otlp]
- OTLP 只保证一对 client/server 节点间的投递，不保证多跳端到端；未收到 ack 后重发可能产生重复数据。[OTLP 限制][otlp]
- 多目的地导出应分别维护 queue、ack 和 retry，避免慢后端阻塞其他后端。[多目的地建议][otlp]

**推断：**稳定 trace/span/observation ID 是重放和去重的必要条件，但 OTLP 不承诺 exactly-once。Langfuse 的具体去重行为仍需实测，不能由中性 Adapter 宣称。

### 4.3 采样（事实与建议）

- Langfuse 默认采集全部；`LANGFUSE_SAMPLE_RATE` / `sample_rate` 是客户端 **trace-level** 采样，未选中的 trace 不发送 observations 或关联 scores。[Langfuse sampling][lf-sampling]
- OTel sampler 属于 `TracerProvider`，在 span 创建时决策；默认 `ParentBased(root=AlwaysOn)`。[OTel sampling][otel-sdk]

**建议：**采样只影响观测副本，不影响 append-only Trajectory。按稳定 trace ID 做一致决策，保留完整 Turn 树。错误优先、tail sampling、离线 eval 是否绕过在线采样是独立产品策略，不要先按 span 随机过滤再期待完整恢复树。

### 4.4 敏感内容（事实与建议）

- 当前 GenAI 规范要求 instructions/input/output 默认不采集、显式 opt-in；生产环境推荐外部内容存储加引用，并可配置截断。Tool arguments/result 也受内容开关控制且可能含敏感信息。[GenAI 内容规则][otel-genai]
- Langfuse Python v5 推荐 `mask_otel_spans`，在导出阶段修改属性；该 hook 失败会丢整个 export batch。它只影响经过该 Langfuse client 的 exporter，其他 exporter 收到的是未修改副本。[Langfuse masking][lf-masking]
- OTel Baggage 会跨服务传播，Langfuse 明确警告不要把密码、API key 或个人数据放入 baggage。[属性传播警告][lf-otel]

**建议：**fan-out 前统一做默认关闭内容的 redaction/truncation，再允许每个目的地增加第二层策略。权威本地日志与导出策略分开，脱敏不得回写 Trajectory。资产优先导出不带凭据的受控引用，而非签名 URL。

## 5. 对 nextloop 现状的适配评估

### 5.1 本地事实与信息缺口

当前 `Event` 包含 `session_id/seq/turn/step/attempt/writer_epoch/event_id/event_type/payload/occurred_at/schema_version`，Store 用 expected seq 提交。Provider 每次 attempt 先产生 started，再以 success/error/cancelled 结束，Driver 随后批量提交这些事件。[事件契约](../../src/nextloop/contracts.py)、[ModelCaller](../../src/nextloop/execute/model.py)

Tool 在 runtime 执行前提交 `tool/call`，结果另行提交；Recovery 会补 interrupted、unknown 和 delivery 事实。[Tool 提交边界](../../src/nextloop/execute/batch.py)、[Recovery](../../src/nextloop/events/repair.py)

基线中的缺口：

1. `occurred_at` 是事件形成或提交附近的 wall-clock 时间，不是完整 operation start/end、首 chunk 或 Provider server 时间，不能精确推出 latency、queue wait 或 TTFT。
2. `assistant/attempt` 不含完整请求快照或 response/model id；`assistant/message` 有规范化 message、finish reason 和 usage，OpenAI/Anthropic Adapter 的输出粒度不同。历史重建不能自动得到更多 wire 细节。
3. Envelope 没有独立 `model_call_id` 或 trace/span ID；`turn/step/attempt` 与 `event_id` 是现有稳定关联基础。
4. EventStore 读接口分 Surface 与 Recovery；离线 eval 若需要全部内容、Compaction 前历史或版本化请求，应使用完整日志，而不是 Conversation Projection。

### 5.2 提交后观测 vs 分散插桩（推断）

| 方案 | 优点 | 风险 / 限制 | 结论 |
|---|---|---|---|
| EventStore 提交后统一观测 | 只看到 committed facts；覆盖 Provider、Tool、Recovery、Delivery；易离线重放；不污染核心接口 | 时间粒度较粗，只得到已记录字段；装饰器需避免递归观测 | **首版推荐** |
| Provider / Tool 内部分散插桩 | 可得精确网络起止、首 chunk、SDK/wire 属性 | 重复或残缺树；恢复后难关联；Provider 语义漂移；导出故障侵入执行；replay 可能重复导出 | **仅作后续窄补充** |
| 两者混合 | 可兼顾事实与细粒度指标 | 必须统一 ID、时间和去重协议 | **暂缓，先证明确有需求** |

推荐顺序：**底层 append 成功 → 获得带 seq/occurred_at 的 committed Events → 构造不可变 observation → 非阻塞 enqueue**。Sink 抛错、队列满或后端慢均不改变 append 返回值。不得在 sink 中再次调用被装饰 Store 写遥测事件。

### 5.3 稳定 ID、retry / recovery / replay（建议）

- 在 Turn 第一次提交时确定并持久关联 `trace_id`，后续进程、Recovery 和离线导出重用。可按 `(installation/namespace, session_id, turn)` 确定性派生 OTel 128-bit trace ID，或把映射持久化；迁移和冲突策略待决策。
- observation/span ID 由稳定逻辑键派生：Turn、Step、attempt、Tool call + intent seq、Delivery final ID。不要使用随机新 ID，否则每次 replay 都制造副本。`event_id` 适合作为点事件幂等键，但不能不加命名空间就截断成 64-bit。
- 一个 attempt 一个 generation；retry 共享父 model-call/Step，不覆盖先前错误。Recovery 不重放 Provider/Tool 动作，只补发缺失 observation，并标记 `recovered=true`、原 writer epoch 和 closer event ID。
- 崩溃留下的开放 span 应按 committed closer 结束；没有结束事实时标为 interrupted/unknown，不用恢复当前时间伪造原操作耗时。
- 离线批量导出以 `(session_id, seq)` 或导出 checkpoint 为游标，可重复发送同一稳定 ID；记录每目的地独立状态。不要把在线 Exporter 的内存 queue 当 durable outbox。

### 5.4 时间与内容（建议）

中性记录应分开携带 `started_at/end_at/observed_at`、时钟来源和精度；只有确实测到才填 `completion_start_time` 或 TTFC。首版可以用 attempt 事件的提交时间作为近似边界，但必须标为 commit-observed latency；若产品需要精确 Provider latency，再增加提交事实或受控 Provider hook。

内容导出采用 `none / metadata-only / redacted / full` 的显式策略，默认 metadata-only。Tool 参数、结果、Provider error body、Prompt、final 和 metadata 都经过同一策略；原始 Trajectory 不变。离线 eval 若需原文，应在获授权的本地数据域读取，不应因 eval 绕过导出脱敏。

## 6. 建议的抽象与能力分层（不含实现）

```text
EventStore.append(...)
  -> authoritative append succeeds
  -> ObservingEventStore emits immutable committed-event batch
  -> ObservationSink (neutral observations; flush/close; per-destination isolation)
       -> OTLP adapter: OTel ids, parentage, time, status, GenAI mappings
       -> Langfuse adapter: langfuse.* mappings, v4 headers, native score/eval capability
       -> custom backend adapter: own capability mapping
```

中性 observation 建议表达：`trace/observation/parent identity`、`kind`、`name`、`start/end/observed time`、`status + domain outcome`、`input/output reference or sanitized value`、`usage`、`attributes`、`source event range`。它是导出 DTO，不是第二个 EventStore Schema，也不进入模型 Projection。

**通用 Adapter 可以承诺：**尽力异步投递、稳定关联、显式时间、内容策略、flush/close 超时、失败诊断、可重放导出，以及各后端明确支持字段的映射。

**不能由通用 Adapter 承诺：**exactly-once、跨后端完整 trace 查询、一致的 eval/score 模型、Prompt/version 管理、annotation、media 上传与访问控制、experiment runner、成本定价、实时性、采样等价、保留期、数据驻留或服务端去重。上述能力应通过可选 capability 暴露；不支持时明确报 `unsupported`，不要静默写入 metadata 冒充成功。

## 7. 待决策与验证顺序

1. **Trace 粒度：**推荐 Turn；若改 Session 级 trace，长会话的 root latency、采样和恢复语义需另定。
2. **ID 来源：**确定性派生还是持久映射；需要覆盖 Session 重建、迁移和多 installation 冲突。
3. **首版精度：**是否接受 commit-observed latency；TTFT、首 chunk、Provider response ID 是否值得增加事实或细粒度 hook。
4. **可靠性目标：**内存 best-effort 足够，还是需要 durable outbox；OTLP 重试不等于持久队列。
5. **内容默认值与合规域：**各环境和后端允许哪些字段，是否允许内容外链，谁管理密钥与保留期。
6. **Eval 权威源：**推荐 nextloop 完整日志；Langfuse native eval 是消费和展示集成，不是原始事实权威。
7. **平台验证：**固定 Langfuse Cloud 或自托管版本后，实测 v4 header、父子树、usage/metadata 映射、重复 ID、partial success、Score 关联和 flush 行为。本文未做这些在线验证。

建议先用内存 fake sink 做确定性映射验证，再以本地 OTel Collector 验证 OTLP/HTTP，最后接 Langfuse 测试项目。验收至少覆盖 retry、protocol correction、unknown Tool、进程恢复、delivery-only replay、重复导出、队列溢出、flush 超时和默认无内容泄漏。

## Primary sources

以下来源均于 **2026-11-09（Asia/Shanghai）** 核验；官方 `main` 文档是本次时点快照，后续变化不自动进入本文结论。

[lf-sdk]: https://langfuse.com/docs/sdk/python/sdk-v3
[lf-otel]: https://langfuse.com/integrations/native/opentelemetry
[lf-model]: https://langfuse.com/docs/observability/data-model
[lf-api]: https://langfuse.com/docs/api-and-data-platform/features/public-api
[lf-score]: https://langfuse.com/docs/evaluation/evaluation-methods/data-model
[lf-experiments]: https://langfuse.com/docs/evaluation/experiments/data-model
[lf-masking]: https://langfuse.com/docs/observability/features/masking
[lf-sampling]: https://langfuse.com/docs/observability/features/sampling
[otel-moved]: https://opentelemetry.io/docs/specs/semconv/gen-ai/
[otel-genai]: https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-spans.md
[otel-inference]: https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/client-inference.md
[otel-agent]: https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-agent-spans.md
[otel-tool]: https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/execute-tool.md
[otel-sdk]: https://opentelemetry.io/docs/specs/otel/trace/sdk/
[otlp]: https://opentelemetry.io/docs/specs/otlp/
