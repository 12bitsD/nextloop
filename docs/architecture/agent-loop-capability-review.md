# AgentLoop 能力分类与架构收敛分析

日期：2026-11-02。对照基线：`57713e41124e2b085fc781c993cfebeb7156fc6a`，加本次 [AgentLoop 内部拆分工作稿](../design/agent-loop.md#12-internal-structure-and-module-interfaces)。来源为用户提供的 Agent Loop / DSH / Super-agent 综述，以及本轮公开一手来源核验。本文是分析与候选清单，不把外部机制直接改为 Nika 已确认需求；功能状态仍由 [roadmap](discussion-roadmap.md)、[worklist](feature-worklist.md) 和各设计文件承载。

用户要求是：完整记录输入，按综述的标准和分类检查现有设计，判断哪些能力有必要补充；整体架构简单，完整功能范围内的细节打磨到位。当前仓库仍无 runtime，实现和运行验收均未开始。下文的“已有”统一指已有设计。

## 1. 核心判断

Nika 已覆盖综述中多数基础方向：原生模型/工具循环、runtime 执行规则、运行中接纳 steer、资产化大输出、缓存与压缩、软预算、持久事件、状态与执行分离、Wiki/Skill，以及取消和故障收尾。主要差距是把这些原则补成完整协议并验证，而非缺少同名模块。

综述最有价值的增量是三件事：

1. 将 **ReAct 的响应判定、行动许可条件、观察回填与出口** 单独作为关闭项。
2. 将 **目标、验证、停止和恢复信息** 连成场景契约，既检查任务结果，也检查无进展和未知状态；保留当前软预算决定。
3. 把 **人的注意力、主动触发、长任务响应性与可信授权** 放到所属专题中做具体取舍，而不是通过 Prompt 暗示自动获得这些能力。

Router/Worker、后台 job、持久事件订阅、独立业务 Watchdog 是可能有价值的新能力，但它们改变调度和生命周期。其价值需要从 Nika 的实际工作场景推导，不能由某个原型的成绩或“更先进”的描述直接决定。

## 2. 外部证据与适用边界

一手核验分别在 [DSH / Super-agent 来源笔记](../research/2026-11-02-dsh-primary-sources.md) 与 [Loop Specification / DSH 社区证据](../research/2026-11-02-loop-evidence-notes.md)。笔记保留固定版本、具体位置、核验范围和未证实项；本轮未运行外部原型或复现其 benchmark。

| 综述中的依据 | 本轮核验后应如何使用 |
|---|---|
| DeepSeek Super-agent 架构与原型 | 官方架构文章、DSH 主仓和独立 Super-agent prototype 必须分开。官方说明属于架构探索，prototype 与文章/内部版本并不完全相同；不能把文章列出的每项能力当成开源代码已有 |
| Router 靠工具集强制委派、外部 Watchdog | 文章提出相关方向，但公开原型仍给 Router 提供 Bash/read 等工具；本轮检查的原型未找到 Watchdog 实现。“结构保证委派”是可以评估的设计原则，不能当作该原型已证明的强制边界 |
| p50=12.4、p95=15.7 分钟 | 官方报告的合成负载结果，每方案 5 次，约 300 条消息/20 个工作请求，coding 用耗时工具模拟。可提示尾延迟和人工协调的价值，不证明真实代码质量或 Nika 的收益 |
| 约 23.2M cache-read tokens | 跨所有 Sessions、每次运行累计的缓存读取量；不是上下文容量，也不能单独换算成完整成本。真实比较要同时记录 uncached input、cached input、output、价格与成功质量 |
| Loop Specification 五要素与 50 个 loop | arXiv:2607.00038 的作者对公开配方进行人工编码。70%/74% 是验证层级与终态写法的样本比例，不是实测成功率或因果结果 |
| 27 步、约百万 token 的 DSH 案例 | 社区讨论提供的个案观察，可作为回归场景来源；不是所有 ReAct 的必然表现，也不是本轮复现结果 |
| 19 Sessions、72–88% uncached prefill 减少 | 社区作者的扩展实验报告，不能称 DSH 官方保证；公开可复现证据的局限见笔记 |

以下一般化结论需要收窄：

- **串行执行工具不意味着无法接收输入。** intake 可以在等待 Tool 时保存 steer、接收回答和发送取消信号；是否让模型同时规划下一步、是否让 Tool 返回 handle 后跨 Turn 运行，是另外的语义。
- **累计输入可能近似平方增长，账单并不必然如此。** 若初始输入为 B，每轮固定增加 d，n 次请求未经压缩的累计输入约为 nB+d·n(n−1)/2。实际费用还取决于前缀缓存、输出、不同工具结果长度和压缩；每次上下文容量也必须单独计算。
- **持久 Session 与常驻实体是不同层次。** Nika 的 Session/Trajectory 持久化和容器部署可以支持用户设备离线后的服务存续；后台服务自身故障后恢复哪些工作仍需协议，历史副作用仍不自动重放。
- **Run 结束、模型停止与业务成功分开。** 退出条件需解释停止原因及可核查结果；没有工具调用不是证明任务完成的充分条件。
- **独立任务并行与强耦合任务拆分分开。** 两个目标独立也可能竞争相同 workspace、登录态或桌面，仍需要资源所有权；单循环执行算法可以被多个 Session 使用，不能据此推导新 Router 的必要性。

## 3. 按综述分类对照 Nika

分类含义：**已有设计**=可沿用但待实现；**契约补齐**=在现有架构内明确接口/所有权/故障；**候选新增**=需场景和取舍决定；**不自动纳入**=与当前明确基线冲突或证据不足。

| 综述能力 | Nika 当前依据 | 归类与收敛动作 |
|---|---|---|
| ReAct + native tool calling | [AgentLoop §5](../design/agent-loop.md#5-tool-calls) 有 model → calls → results → model | **已有骨架 / 契约补齐**：补下节 G0。保持模型决策与 runtime 执行分工，不要求明文 Thought 标签或新增 Reasoner |
| Harness 规则由结构保证 | [内部结构](../design/agent-loop.md#12-internal-structure-and-module-interfaces) 已分 driver/input/model/tool-batch/finalization | **契约补齐**：工具 schema、权威身份、授权、资源、写入顺序与取消不能只靠 Prompt；不为此增加通用插件框架 |
| 可替换 Loop | Session 持久、Loop 按需创建，外部依赖分责 | **已有替换基础**：保持小接口和可独立验证即可；当前不需要动态 Loop 插件系统 |
| Inbox、中途插话 | Session-owned 单 steer buffer、控制输入绕行、WaitingRequest | **已有设计 / 契约补齐**：接纳、注入、重启积压与回答路由；独立任务 FIFO 或 send/followup/steer/inject 四入口不自动照搬 |
| 大输出 spill 与按需读取 | [内容访问](../design/context-builder.md#content-access-and-task-performance)、asset-before-reference | **已有设计 / 契约补齐**：全文必须可恢复，preview/分页/定位/引用寿命明确；不照搬 50KB 或按实验百分比承诺收益 |
| 外部知识与少量通用工具 | AgentWiki grep/read、Skill 按需加载、静态 Tool Catalog | **已有设计**：保留飞书/Browser/Computer 所需明确工具；工具数量由可用性与完整契约决定，不机械缩成只有 shell |
| 验证和停止 | System Prompt 验证规则、最终提交/投递、取消和未知结果 | **契约补齐**：任务验证如何形成证据，如何表达未完成、受阻、停止；不让万能 Verifier 仅凭模型文字判断成功 |
| 步数/时间/成本预算 | 当前调用数与时间为软预算，usage/preflight 为已有设计 | **已有设计 + 候选新增**：可补软成本观测与无进展提示。硬步数/时间/成本闸与当前选择不同，需明确变更才采用 |
| 外层 Loop Specification | 输入、用户目标、验证规则、取消、Trajectory/Wiki 已各有归属 | **可直接作为评审分类**：每场景检查 trigger/goal/verification/stopping/memory；持久自动重试/轮询外层 Loop 需另行决定 |
| Router/Worker、多 Agent | 一套原生 Loop；多 Session 资源隔离待设计 | **候选新增**：先量化并行独立任务和统一出口的收益；自动分派、worker 池、subagent 需授权继承、资源、汇总和取消协议，当前不加入 |
| 事件触发、代码过滤、observe-first | Feishu 事件输入已有；Proactive 价值已接受 | **Proactive 候选**：先定义允许的事件、规则和只记录候选动作的观察阶段；不能把 observe 记录自动升级为执行授权 |
| 跟进订阅直连 | 已有入口→Session 固定映射 | **部分已有 / 候选新增**：已知 Thread 的后续输入可沿原映射；任务结束后持久订阅并唤醒、新增 GitHub/邮件/CI 来源是新的生命周期。评论仍沿严格 @ 默认 |
| 长工具异步化 | 等待 Tool 时 intake/stop 应可响应，工具首版串行 | **响应性属契约补齐，后台 job 属候选新增**：先把取消、等待和进度做对；跨 Turn handle、完成事件、重启接管与 orphan 另定 |
| Ticket + immutable Mandate | Runtime 派生可信授权；Wiki 三种 scope | **借鉴授权留痕**：记录发起人、来源、范围、策略版本和有效期；扩权经新的可信输入追加。不可变目标、扩权必建 Ticket 及第二任务库不自动采用 |
| 外部 Watchdog | SessionManager 启动恢复、lease、进程监督待工程化 | **拆成两项**：存活/失联/孤儿资源检测属于 M6/M8；判断业务偏航、干预模型或重启任务的监督 Agent 是候选新增 |
| 人类注意力预算 | Proactive 减少上下文负担的目标已有 | **专题待设计**：什么值得通知、如何合并、哪些请求要阻塞、何时静默；用户主动问状态时仍返回真实状态 |
| 状态/计算/环境分离 | Session/Trajectory、按需 Loop、容器内执行资源 | **已有原则 / 契约补齐**：资源 owner、重启清理和可恢复事实；不扩大到远程设备或隐含跨机器任务恢复 |
| 链式召回、投机执行 | 稳定引用/按需检索有基础，投机副作用未设计 | **不自动纳入**：沿真实来源链接检索可复用；未确认前启动写入/外部动作需要独立授权与收益证据 |

<a id="react-execution-protocol-gap"></a>

## 4. G0：ReAct 执行协议记录

2026-11-02 用户指出此前 AgentLoop 没有单独讨论 ReAct；核查确认已有循环骨架，但模型输出到运行决策的协议未完整定义。此前“M4 Done”只适用于已有状态机/生命周期基线，不能代表此协议已关闭。该缺口应先于或结合 Tool Catalog 收敛。

2026-11-03 已确认：[正常结束前检查 pending steer](../design/agent-loop.md#normal-turn-completion)、[普通工具错误后的同批继续](../design/agent-loop.md#tool-batch-failure-policy)，以及 [超时与结果未知的处理](../design/agent-loop.md#tool-timeout-outcomes)。已接纳、未注入的 steer 继续同 Turn/Run；final commit 后接纳的输入归下一 Turn。普通错误提交后继续同批调用；有结果或成功依赖的动作分两次模型调用。超时导致 unknown 则结束本批，提交后仅在 Runtime 允许时同 Turn 核查，否则收尾报告受阻，保留资源限制。具体事务、逐工具格式与资源机制及运行验证待实现；下表保留其余协议缺口。

同日进一步确认 [整批执行前校验](../design/agent-loop.md#tool-call-validation)：任一名称/参数/Schema、身份或完整性错误都使本批全部未执行并报错。普通错误续批仅适用于通过校验后的运行错误；报错后的模型修正协议仍待收敛。

| 契约 | 必须覆盖 | 关闭产物 |
|---|---|---|
| 响应分类 | text、tool calls、混合输出、空响应、refusal、长度截断、流中断与 Provider 终止信号；哪些文字属于过程或最终交付 | Provider 原生响应 → 统一逻辑结果 → driver 动作表；不从自然语言“完成了”猜终态 |
| Action 可执行条件 | 整批执行前校验及错误即整批拒绝已定；逐 Provider 的完整性/身份映射、逐工具 schema、坏 JSON/截断等错误的合法反馈 | 执行前零副作用和整批拒绝的验证；错误如何进入模型修正请求仍待 T1/T2 定义 |
| Observation | 成功、失败、部分成功、未知、取消的逐工具字段/证据；Tool Call/Result 配对；大输出和多模态引用；已定超时/unknown 规则的 Provider 映射和资源准入 | 可回放为合法模型消息的观察样例；普通错误续调、同批继续及实时超时/unknown 行为已定，不能因映射丢失未知信息；持久化失败单独处理 |
| 继续与结束 | Tool Result 后续调、WaitingRequest、无输出/截断恢复、最终回复、显式停止、终止错误、无进展 | 决策表与失败路径；业务验收证据独立于模型 end_turn，保持软预算不强停 |
| 推理相关字段 | API 公开 summary、需原样回传的 opaque/signature 内容、普通正文；各 Provider 续调与压缩要求 | 只处理 Provider 合法提供的数据，不要求完整隐藏思维链，不混入用户回复或 WorkingContext |

建议落点为现有 model invocation 与 driver 的接口、Tool batch 的准入/结果契约、ContextBuilder 的投影。名称、数据类型和各错误分支仍是待审方案；本次记录不选定框架、不新增第二个执行循环。

## 5. 值得优先补齐的能力与关闭条件

以下均为 **state=proposed** 的收敛建议。除已有明确契约的同步外，不自动改变 runtime 基线。

| 候选 | 建议深度与所属模块 | 最小关闭条件 |
|---|---|---|
| P1 验证驱动的结果交付 | 为复杂任务明确目标、范围、验证方法、证据和未完成项，复用用户输入/Skill/Trajectory；Task 定义不另存一套权威状态。Loop 确保结果可回传，具体 Skill/Agent 选择任务相关的回归范围 | 能访问并执行足够的检查；测试范围由影响面决定，不固定每轮全跑。未跑/失败/环境受阻如实记录；业务成功有外部结果证据 |
| P2 无进展与成本观测 | 同类 Tool 调用、结果变化、等待类型、已花时间和 usage 形成可解释诊断；先提供软提醒。expected polling、合法长等待与真正重复探索分开 | 重复检索、长测试、等回答三种场景不混判；提醒不会自动取消或重放。成本缺价格/usage 时标 unknown，价格带来源版本 |
| P3 授权来源绑定 | M6 定义真实用户/事件来源、授权范围与策略版本，runtime 计算有效权限。目标可被 steer 改写，权限扩大仍要可信授权；所有版本追加留痕 | 用户中途改目标不会被旧 Ticket 锁死；文档/工具内容不能自授扩权；凭据使用路径不要求模型获得明文 |
| P4 主动性与注意力协议 | Proactive 采用具体触发场景和观察阶段候选，定义内部整理、用户交付、决策请求、异常通知。后台唤醒仍待选择 | 同一事件不重复通知；只记录阶段不执行；输出与真实进展相关；通知不形成新的动作授权 |
| P5 可响应的长操作与健康诊断 | ToolRuntime 在等待时保留取消/输入通道，记录进度证据；SessionManager/进程监督检查存活和资源归属 | 长工具期间可接收 steer/stop；停止结果区分已退出与未知；恢复不重放副作用。后台 job 和业务监督 Agent 不在此候选中隐式加入 |

采用新的硬预算、Router/Worker、持久订阅、跨 Turn 后台 job 或目标级 Watchdog，需要分别解释用户场景、共享资源、额外状态、成本和可验收收益。它们不能因为被列入综述而自动成为完整交付范围的一部分。

## 6. 用 Loop Specification 五要素关闭场景

把五要素作为审查表，足以发现跨层空缺；暂不新增可执行 DSL、自动循环引擎或持久 TaskSpec 数据库。

| 要素 | Nika 中需要说明的内容 | 当前归属 |
|---|---|---|
| Trigger | 入口、事件身份、去重、谁触发、是否已有授权、是否仅观察 | Feishu ingress；Proactive 专题 |
| Goal | 当前请求、完成标准、允许范围、steer 如何更新；扩权与改目标分开 | 用户输入、Session projection、Skill；授权由 Runtime |
| Verification | 用什么产物/检查证明结果，失败怎样反馈，哪些证据仍缺失 | 具体任务 Skill/Tool/Agent；Trajectory 保存实际结果 |
| Stopping rule | 何时输出结果、等待回答、取消或因故障停止；如何呈现部分完成 | ReAct 协议、driver、WaitingRequest、finalization |
| Memory | 恢复当前任务需要哪些 committed facts；什么可进长期 Wiki，谁授权 | Session events/assets、ContextBuilder、AgentWiki |

必须联合走查的例子：代码修改造成回归；重复搜索却无新证据；工具等待时用户改方向；提交结果前崩溃；发送成功但回执丢失；截图资产被 GC；事件触发超过授权；任务有结果但连续重复通知。每例明确 owner、事件、结果和异常，不能仅写一句“支持”。

<a id="closure-gap-index"></a>

## 7. 统一 gap 索引与讨论归属

此处登记对话已列出的 G0–G13，维持原 roadmap 所属阶段。用户随后要求按大 topic/部件归组，具体未定问题、跨模块接口与讨论顺序统一维护在 [roadmap 的 T1–T8](discussion-roadmap.md#discussion-topics)；下表只作 gap→主 topic 映射，不另建排期。以下均为待收敛/待验证，非实现状态。

| ID | Gap | 主讨论 topic | 关闭条件与原阶段 |
|---|---|---|---|
| G0 | ReAct 执行协议 | [T1 执行控制](discussion-roadmap.md#topic-execution) | §4 的五项协议与完整场景表；M4 follow-up，衔接 M5 |
| G1 | Tool Catalog | [T3 工具/权限/资源](discussion-roadmap.md#topic-tools) | 每个工具 schema、description、错误/取消/大结果示例；M5 |
| G2 | Loop 内部接口与输入接纳 | [T1 执行控制](discussion-roadmap.md#topic-execution) | 唯一 writer 与提交责任，等待回答/steer/stop/积压输入可确定归属；M6–M8 |
| G3 | Context / Provider 实现契约 | [T2 模型/上下文](discussion-roadmap.md#topic-context) | 确定请求、部分输出、wire 映射、压缩自身容量与校验；M5/M8 |
| G4 | 操作者权限与执行凭据 | [T3 工具/权限/资源](discussion-roadmap.md#topic-tools) | 授权来源、作用域、失效与凭据访问、Wiki 写入不可绕过；M6 |
| G5 | 共享资源与 Browser/Computer | [T3 工具/权限/资源](discussion-roadmap.md#topic-tools) | workspace/profile/GUI/Wiki 的 owner、锁、取消、失联与未知；M6/M8 |
| G6 | 事件、持久化与恢复 | [T4 持久状态](discussion-roadmap.md#topic-state) | schema、事务、幂等、lease、投影/运行状态 fold、损坏处理；M7 |
| G7 | Wiki / Skills 写入与发布 | [T6 知识/行为资产](discussion-roadmap.md#topic-knowledge) | 授权→验证→写入→Git/Trajectory→发布/失败，动态 Skill 兼容；M6/M7 |
| G8 | 飞书输入、文档与评论 | [T5 渠道/交互](discussion-roadmap.md#topic-channels) | 每种入口的去重/延迟/编辑冲突/部分成功/回读协议；M7 |
| G9 | 投递与交互呈现 | [T5 渠道/交互](discussion-roadmap.md#topic-channels) | final_message_id、接管、delivery_unknown、过期回答、长文与状态；M7 |
| G10 | Assets、查询导出与数据生命周期 | [T4 持久状态](discussion-roadmap.md#topic-state) | 引用完整、导出完整性、版本化格式、一致备份、GC/磁盘满；M7/M8 |
| G11 | Proactive | [T7 主动性/注意力](discussion-roadmap.md#topic-proactive) | 触发、授权内行为、观察/执行、通知、去重与取消；独立专题 |
| G12 | 工程、部署与运行诊断 | [T8 工程/验收](discussion-roadmap.md#topic-engineering) | 语言/接口/依赖、进程管理、配置、升级迁移、健康与 CI；M8 |
| G13 | 全功能验收与实施分期 | [T8 工程/验收](discussion-roadmap.md#topic-engineering) | 每能力的正常/失败/并发/恢复场景、证据、退出条件与完整覆盖；M9 |

当前从 T1 的 G0 与 T3 的 G1 联合讨论开始，后续顺序见 roadmap。P1–P5 的主归属也统一登记在那里。已有 WorkingContext、缓存、usage fallback、压缩 Prompt 与软预算机制不重新选型；具体数据结构、接口和验证仍按归属补齐。实施分期保留全部已确认能力；候选新增能力须先经过明确选择。

## 8. 本轮记录与变更边界

- 保存用户综述原文与所有给出的链接，在 Work Wiki 登记来源和本分析入口；未将二手叙述直接提升为长期确定事实。
- 记录 ReAct 缺口，并让 roadmap/worklist 明确区分 M4 原状态机基线与新增协议关闭项。
- 按既有决定同步软预算、完整 preflight 条件、工具串行和已定 steer 边界等旧句子，保留本次分析前的版本基线可供追溯。
- 研究文件记录外部作者的证据，不声称本轮复现；能力建议保持 proposed。
- 变更范围为设计与研究文档；未创建 runtime、自动任务、订阅或后台监督。后续按模块讨论的入口统一维护在 roadmap，文档发布不表示候选能力已采纳或实现。
