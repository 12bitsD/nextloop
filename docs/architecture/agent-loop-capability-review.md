# AgentLoop 能力分类与架构收敛分析

> **历史评审记录，日期：2026-11-02。** 本文保留当时的缺口分析和候选比较，不代表当前进度或纯串行 Tool 决策。当前架构见 [DSH-aligned AgentLoop](../design/agent-loop.md) 与 [ADR-0002](../adr/0002-dsh-aligned-agent-loop.md)。

对照基线：`57713e41124e2b085fc781c993cfebeb7156fc6a`，以及当时的 [AgentLoop 内部拆分工作稿](../design/agent-loop.md)。来源为用户提供的 Agent Loop / DSH / Super-agent 综述和公开一手资料。当时仓库尚无 runtime，下文的“已有”均指已有设计，不代表已经实现或验证。

功能状态以 [roadmap](discussion-roadmap.md)、[worklist](feature-worklist.md) 和当前设计文档为准。

## 1. 核心判断

当时设计已覆盖多数基础方向：模型/工具循环、执行规则、运行中 steer、大输出、缓存与压缩、软预算、持久事件、状态与执行分离、Wiki/Skill、取消和故障收尾。主要缺口是补全协议并验证，不是增加同名模块。

综述的主要增量有三点：

1. 单独定义 **ReAct 的响应判定、行动条件、观察回填和退出规则**。
2. 将 **目标、验证、停止和恢复** 连成场景契约，同时检查结果、无进展和未知状态。
3. 分别讨论 **人的注意力、主动触发、长任务响应和可信授权**，不通过 Prompt 假定这些能力已经存在。

Router/Worker、后台 job、持久事件订阅和业务 Watchdog 可能有用，但会改变调度和生命周期。是否采用应由实际场景和收益决定，而不是由原型成绩决定。

## 2. 外部证据与边界

一手核验见 [DSH / Super-agent 来源笔记](../research/2026-11-02-dsh-primary-sources.md) 和 [Loop Specification / DSH 社区证据](../research/2026-11-02-loop-evidence-notes.md)。本轮未运行外部原型，也未复现其 benchmark。

| 综述依据 | 本次核验后的使用方式 |
|---|---|
| DeepSeek Super-agent 架构与原型 | 官方文章、DSH 主仓和独立 prototype 必须分开。文章属于架构探索，不能据此认定所有能力已在开源代码中实现 |
| Router 工具集和外部 Watchdog | 文章提出了方向，但公开原型仍给 Router 提供 Bash/read 等工具，未找到 Watchdog 实现。不能宣称原型已证明强制委派边界 |
| p50=12.4、p95=15.7 分钟 | 官方合成负载结果：每方案 5 次，约 300 条消息、20 个工作请求，coding 使用模拟工具。只能提示尾延迟和人工协调价值，不能证明代码质量或 Nika 收益 |
| 约 23.2M cache-read tokens | 多次运行、多个 Session 的累计缓存读取量，不是上下文容量，也不能单独换算成本。比较时还需 uncached input、cached input、output、价格和结果质量 |
| Loop Specification 五要素与 50 个 loop | arXiv:2607.00038 作者对公开配方的人工编码。70%/74% 是样本比例，不是实测成功率或因果关系 |
| 27 步、约百万 token 的 DSH 案例 | 社区个案，可作为回归场景来源，不代表所有 ReAct 的表现，也不是本轮复现结果 |
| 19 Sessions、72–88% uncached prefill 减少 | 社区作者的扩展实验，不是 DSH 官方保证；复现限制见来源笔记 |

以下结论需要限定范围：

- **串行 Tool 不等于无法接收输入。** 等待 Tool 时仍可保存 steer、接收回答和取消。模型是否并行规划、Tool 是否跨 Turn 返回 handle，是另外的设计问题。
- **输入量近似平方增长，不代表账单也如此。** 若初始输入为 B，每轮增加 d，n 次未压缩请求的累计输入约为 `nB + d·n(n−1)/2`。实际费用还取决于缓存、输出、Tool 结果和 Compaction。
- **持久 Session 不等于常驻服务。** 持久化和容器部署可以让服务在用户离线后存续，但故障后如何恢复工作仍需协议，历史副作用不自动重放。
- **Run 结束不等于业务成功。** 停止原因和结果证据需要单独检查；没有 Tool Call 也不能证明任务完成。
- **任务独立不等于没有资源冲突。** 独立任务仍可能共享 workspace、登录态或桌面。一个 Loop 可服务多个 Session，但不能据此推导必须有 Router。

## 3. 按综述分类对照 Nika

分类含义：**已有设计** = 可沿用但待实现；**契约补齐** = 需要明确接口、所有权和故障语义；**候选新增** = 需要场景和取舍；**不自动纳入** = 与当前基线冲突或证据不足。

| 综述能力 | 当时依据 | 归类与处理 |
|---|---|---|
| ReAct + native tool calling | [AgentLoop](../design/agent-loop.md) 有 model → calls → results → model | **已有骨架 / 契约补齐**：补 G0。保持模型决策与 runtime 执行分工，不要求明文 Thought 或新增 Reasoner |
| Harness 规则由结构保证 | 内部已分 driver/input/model/tool-batch/finalization | **契约补齐**：Schema、身份、授权、资源、写入顺序和取消不能只靠 Prompt；不增加通用插件框架 |
| 可替换 Loop | Session 持久，Loop 按需创建，外部依赖分责 | **已有替换基础**：保持小接口和独立验证，不需要动态 Loop 插件系统 |
| Inbox、中途插话 | Session 单 steer buffer、控制输入、WaitingRequest | **已有设计 / 契约补齐**：定义接纳、注入、重启积压和回答路由，不照搬独立任务 FIFO 或四种输入入口 |
| 大输出 spill 与按需读取 | [内容访问](../design/context-builder.md)、asset-before-reference | **已有设计 / 契约补齐**：全文可恢复，明确 preview、分页、定位和引用寿命，不照搬固定大小或收益百分比 |
| 外部知识与少量通用工具 | AgentWiki grep/read、Skill 按需加载、静态 Tool Catalog | **已有设计**：保留明确工具，数量由可用性和契约完整性决定，不机械缩成 shell |
| 验证和停止 | System Prompt 验证规则、最终提交/投递、取消和未知结果 | **契约补齐**：明确验证证据、未完成、受阻和停止，不让万能 Verifier 仅凭模型文字判断成功 |
| 步数/时间/成本预算 | 调用数与时间为软预算，usage/preflight 已有设计 | **已有设计 + 候选新增**：可补成本观测和无进展提示。硬预算需要另行决定 |
| 外层 Loop Specification | 输入、目标、验证、取消、Trajectory/Wiki 各有归属 | **可作为评审分类**：逐场景检查 trigger/goal/verification/stopping/memory；持久自动重试和轮询另定 |
| Router/Worker、多 Agent | 一套原生 Loop，多 Session 资源隔离待设计 | **候选新增**：先量化独立任务和统一出口的收益；自动分派、worker 池、subagent 暂不加入 |
| 事件触发、代码过滤、observe-first | Feishu 事件输入已有，Proactive 价值已接受 | **Proactive 候选**：先定义事件、规则和只记录的观察阶段；观察记录不自动获得执行授权 |
| 跟进订阅直连 | 入口到 Session 固定映射 | **部分已有 / 候选新增**：已知 Thread 可沿原映射；持久订阅、唤醒和新增来源属于新生命周期 |
| 长工具异步化 | 等待 Tool 时 intake/stop 应可响应，首版串行 | **响应性属契约补齐，后台 job 属候选新增**：先做好取消、等待和进度；跨 Turn handle、接管和 orphan 另定 |
| Ticket + immutable Mandate | Runtime 派生可信授权，Wiki 三种 scope | **借鉴授权留痕**：记录来源、范围、策略版本和有效期，扩权通过可信输入追加；不自动采用第二任务库 |
| 外部 Watchdog | SessionManager 恢复、lease、进程监督待工程化 | **拆成两项**：存活和孤儿资源检测属于工程能力；业务监督 Agent 属于候选新增 |
| 人类注意力预算 | Proactive 减少上下文负担的目标已有 | **专题待设计**：定义通知、合并、阻塞和静默；用户询问时仍返回真实状态 |
| 状态/计算/环境分离 | Session/Trajectory、按需 Loop、容器内资源 | **已有原则 / 契约补齐**：明确资源 owner、重启清理和可恢复事实，不扩展到远程设备或跨机器恢复 |
| 链式召回、投机执行 | 稳定引用和按需检索有基础，投机副作用未设计 | **不自动纳入**：可复用真实来源检索；未确认前的写入和外部动作需要独立授权与收益证据 |

<a id="react-execution-protocol-gap"></a>

## 4. G0：ReAct 执行协议记录

2026-11-02 核查发现：循环骨架已有，但模型输出到运行决策的协议不完整。此前“M4 Done”只覆盖状态机基线，不代表 ReAct 协议已关闭。

2026-11-03 已确认：

- 正常结束前检查 Pending steer；final commit 后接纳的输入归下一 Turn。
- 普通工具错误后继续同批调用；有结果依赖的动作分两次模型调用。
- 超时导致 unknown 时结束本批。仅在 Runtime 允许时同 Turn 核查，否则收尾为受阻并保留资源限制。
- 任一名称、参数、Schema、身份或完整性错误都会使整批零执行。普通错误续批只适用于通过校验后的运行错误。

具体事务、Tool 格式、资源机制和运行验证当时仍待实现。其余协议缺口如下：

| 契约 | 必须覆盖 | 关闭产物 |
|---|---|---|
| 响应分类 | text、tool calls、混合输出、空响应、refusal、长度截断、流中断和 Provider 终止信号；区分过程文字与最终交付 | Provider 响应 → 统一逻辑结果 → driver 动作表；不根据“完成了”猜终态 |
| Action 可执行条件 | 整批校验、Provider 完整性和身份映射、逐工具 Schema、坏 JSON 和截断反馈 | 执行前零副作用和整批拒绝验证；模型修正请求由 T1/T2 继续定义 |
| Observation | 成功、失败、部分成功、未知和取消；Call/Result 配对；大输出、多模态、超时映射和资源准入 | 可回放为合法模型消息的观察样例；映射不能丢失未知信息，持久化失败单独处理 |
| 继续与结束 | Tool Result 后续调、WaitingRequest、无输出/截断恢复、最终回复、停止、终止错误和无进展 | 决策表与失败路径；业务验收独立于模型 end_turn，保持软预算不强停 |
| 推理相关字段 | API 公开 summary、需原样回传的 opaque/signature 内容和普通正文；各 Provider 续调与压缩要求 | 只处理 Provider 合法提供的数据，不要求完整隐藏思维链，不混入用户回复或 WorkingContext |

建议落在 model invocation、driver、Tool batch 和 ContextBuilder 的既有接口上。本文不选定框架，也不增加第二个执行循环。

## 5. 优先候选与关闭条件

以下当时均为 **state=proposed**，不自动改变 runtime 基线。

| 候选 | 建议内容 | 最小关闭条件 |
|---|---|---|
| P1 验证驱动的结果交付 | 明确目标、范围、验证方法、证据和未完成项，复用用户输入、Skill 和 Trajectory，不另建 Task 权威状态 | 能执行足够的检查，范围由影响面决定；未跑、失败和环境受阻如实记录；业务成功有外部证据 |
| P2 无进展与成本观测 | 记录重复 Tool、结果变化、等待类型、耗时和 usage，先提供软提醒 | 区分重复检索、长测试和等回答；提醒不自动取消或重放；缺少价格或 usage 时标 unknown，价格注明来源版本 |
| P3 授权来源绑定 | 定义用户/事件来源、授权范围和策略版本，由 Runtime 计算有效权限，所有版本追加留痕 | 改目标不受旧 Ticket 限制；文档和工具内容不能自授扩权；模型不必获得明文凭据 |
| P4 主动性与注意力协议 | 定义触发场景、观察阶段、内部整理、用户交付、决策请求和异常通知 | 同一事件不重复通知；观察阶段不执行；输出对应真实进展；通知不产生动作授权 |
| P5 可响应的长操作与健康诊断 | 等待时保留取消和输入通道，记录进度，检查进程存活和资源归属 | 长工具期间可接收 steer/stop；区分已退出和未知；恢复不重放副作用；不隐式加入后台 job 或业务监督 Agent |

采用硬预算、Router/Worker、持久订阅、跨 Turn 后台 job 或目标级 Watchdog，需要分别说明场景、共享资源、额外状态、成本和可验收收益，不能仅因综述列出就纳入范围。

## 6. 用 Loop Specification 五要素检查场景

五要素可作为审查表，不需要新增 DSL、自动循环引擎或持久 TaskSpec 数据库。

| 要素 | 需要说明的内容 | 当时归属 |
|---|---|---|
| Trigger | 入口、事件身份、去重、触发者、授权和是否仅观察 | Feishu ingress；Proactive 专题 |
| Goal | 当前请求、完成标准、允许范围、steer 更新；扩权与改目标分开 | 用户输入、Session Projection、Skill；授权由 Runtime |
| Verification | 用什么产物或检查证明结果，失败如何反馈，哪些证据仍缺失 | 具体任务 Skill/Tool/Agent；Trajectory 保存实际结果 |
| Stopping rule | 何时交付、等待回答、取消或因故障停止，如何呈现部分完成 | ReAct 协议、driver、WaitingRequest、finalization |
| Memory | 恢复任务需要哪些 committed facts，什么进入长期 Wiki，谁授权 | Session events/assets、ContextBuilder、AgentWiki |

需要联合走查的例子：代码修改造成回归；重复搜索没有新证据；工具等待时用户改方向；提交结果前崩溃；发送成功但回执丢失；截图资产被 GC；事件触发超过授权；任务有结果但连续重复通知。每例都要明确 owner、事件、结果和异常，不能只写“支持”。

<a id="closure-gap-index"></a>

## 7. Gap 索引

以下 G0–G13 是当时待收敛和待验证的条目，不表示当前实现状态。主归属沿用原 roadmap，不另建排期。

| ID | Gap | 主讨论 topic | 当时的关闭条件与阶段 |
|---|---|---|---|
| G0 | ReAct 执行协议 | [T1 执行控制](discussion-roadmap.md) | §4 的五项协议与完整场景表；M4 follow-up，衔接 M5 |
| G1 | Tool Catalog | [T3 工具/权限/资源](discussion-roadmap.md) | 每个工具的 schema、description、错误、取消和大结果示例；M5 |
| G2 | Loop 内部接口与输入接纳 | [T1 执行控制](discussion-roadmap.md) | 唯一 writer 与提交责任，等待回答、steer、stop 和积压输入的归属；M6–M8 |
| G3 | Context / Provider 实现契约 | [T2 模型/上下文](discussion-roadmap.md) | 确定请求、部分输出、wire 映射、压缩容量和校验；M5/M8 |
| G4 | 操作者权限与执行凭据 | [T3 工具/权限/资源](discussion-roadmap.md) | 授权来源、作用域、失效和凭据访问；Wiki 写入不可绕过；M6 |
| G5 | 共享资源与 Browser/Computer | [T3 工具/权限/资源](discussion-roadmap.md) | workspace、profile、GUI、Wiki 的 owner、锁、取消、失联和未知；M6/M8 |
| G6 | 事件、持久化与恢复 | [T4 持久状态](discussion-roadmap.md) | schema、事务、幂等、lease、投影、运行状态 fold 和损坏处理；M7 |
| G7 | Wiki / Skills 写入与发布 | [T6 知识/行为资产](discussion-roadmap.md) | 授权、验证、写入、Git/Trajectory、发布和失败；动态 Skill 兼容；M6/M7 |
| G8 | 飞书输入、文档与评论 | [T5 渠道/交互](discussion-roadmap.md) | 每种入口的去重、延迟、编辑冲突、部分成功和回读；M7 |
| G9 | 投递与交互呈现 | [T5 渠道/交互](discussion-roadmap.md) | final_message_id、接管、delivery_unknown、过期回答、长文和状态；M7 |
| G10 | Assets、查询导出与数据生命周期 | [T4 持久状态](discussion-roadmap.md) | 引用完整、导出完整、版本化格式、一致备份、GC 和磁盘满；M7/M8 |
| G11 | Proactive | [T7 主动性/注意力](discussion-roadmap.md) | 触发、授权内行为、观察/执行、通知、去重和取消；独立专题 |
| G12 | 工程、部署与运行诊断 | [T8 工程/验收](discussion-roadmap.md) | 语言、接口、依赖、进程管理、配置、升级迁移、健康和 CI；M8 |
| G13 | 全功能验收与实施分期 | [T8 工程/验收](discussion-roadmap.md) | 每能力的正常、失败、并发和恢复场景，以及退出条件；M9 |

当时从 T1 的 G0 与 T3 的 G1 联合讨论开始。已有 WorkingContext、缓存、usage fallback、压缩 Prompt 和软预算不重新选型；数据结构、接口和验证按归属补齐。候选新增能力必须先明确选择。

## 8. 记录与变更边界

- 保存用户综述和链接，在 Work Wiki 登记来源和本分析入口，不把二手叙述直接作为确定事实。
- 记录 ReAct 缺口，区分 M4 状态机基线与新增协议关闭项。
- 同步软预算、preflight、工具串行和 steer 边界等旧表述，保留版本基线供追溯。
- 研究文件只记录外部作者证据，不声称本轮复现；能力建议保持 proposed。
- 当时只变更设计与研究文档，未创建 runtime、自动任务、订阅或后台监督。文档发布不代表候选能力已采纳或实现。
