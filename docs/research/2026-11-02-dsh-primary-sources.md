# DSH / Super-agent：一手来源核验与 Nika 适用边界

核验日期：2026-11-02（Asia/Shanghai）。范围：DeepSeek Harness、Super-agent 文章和公开研究原型。本文只记录来源和分析，不改变 Nika 的架构或执行授权。本轮未运行 DSH 或原型，也未复现实验。

## 1. 三种材料不能混用

| 材料 | 固定版本 / 日期 | 能证明什么 |
|---|---|---|
| `deepseek-ai/deepseek-harness` | `ce7073d51254c26cd63b22a60835b8f948deb447`，2026-10-29 | DSH 基础 harness 的公开接口、实现和安全声明 |
| *Exploring Super-agent Design* | 官方文章，发布元数据为 2026-11-01 11:37:54 UTC | 作者的设计主张、内部实验报告和开放问题；文章不能用 Git SHA 固定 |
| `deepseek-ai/dsh-superagent-prototype` | `6337fc823ce20251c8de8085948165c81aaa701e`，2026-10-31 | 基于 DSH 的公开应用原型；不等于基础 harness 或文章中的全部设计 |

DSH 自称以 Cordis 为基础、Everything is a Plugin 的开发者预览，接口可能变化。[DSH README][dsh-readme]

原型明确声明：它仅供研究参考，不是 DeepSeek 正式产品，大量工程由 coding agents 完成，尚不适合生产。公开版经过改编，与内部实现和文章不完全一致，部分设计尚未实现。[原型 README][prototype-readme]

文章讨论的是架构思想，不是现有 DSH 功能清单。因此，“文章提出”“源码存在”“作者报告实验”是三类不同证据。[文章导言][article]

## 2. 机制核验

### 2.1 Router / Worker：角色名称不等于权限边界

文章 §2 提出长期 Router Session：积累跨入口上下文、识别任务并协调独立 Workers。Router 不执行实际任务，也不是每步拆解任务的传统 Team Lead。Worker 隔离上下文、自行规划，必要时启动子 Agent。外部沟通由 Router 汇总；Worker 通过订阅共享来源上下文，不是任意互发消息。模型推理期间的 Router 输入可以批量进入下一轮。[文章 §2–3][article]

公开原型确实有 Main Session、Executor Session、Ticket 和 Assignment。Router Prompt 要求把调查和实现交给 Worker，不做实现计划，也不通过 sleep/polling 等待。[领域模型][domain]、[Router prompt][orchestrator-prompt]

但“Router 不读代码”只是 Prompt 规则。架构说明仍给 Router 提供 Bash、read、read_image、jobs 等工具。[Router 工具边界][router-tools] 因此，不能宣称公开原型已经通过代码剥夺 Router 的执行权限。

### 2.2 Ticket / Mandate：授权来源与执行身份分离

原型的链路是 `Trigger → Eval → Ticket / Mandate → Assignment / Executor → Channel`：[领域定义][domain]

- Trigger 保存不可变、可去重的外部事实；Thread 组织共同上下文。
- Ticket 是工作身份，与某次执行是否活跃无关；Assignment 绑定具体执行实例。
- Mandate 保存授权出处，包括 Policy 版本、范围、强度、命中规则、actor 和接收方式。超范围需要新 Ticket，已结束 Ticket 续跑前重新验证授权。
- Subscription 允许多个 Ticket 关注同一 Thread。上下文共享不产生新授权。

`createTicket` 会写入 Ticket/Mandate、订阅 Threads，并在确认后启动 executor。attach 会拒绝人工停止的 Ticket，并重新验证已结束 Ticket 的 mandate。[Ticket 创建与续接][tickets]

可借鉴的是授权可追溯和执行身份分离。Nika 是否需要独立 Mandate 对象仍是设计选择。“授权出处不可变”也不等于用户不能通过后续 steer 修改目标。

### 2.3 Subscription / Pass-through：减少路由，不增加权限

原型 intake 先去重 Trigger、检查来源 scope，再记录输入并唤醒订阅者。只有没有 Ticket 拥有该输入时，才进入新 Eval。context-only 输入不恢复已停止执行，人工停止的 Ticket 不会被普通订阅消息复活。[intake 实现][intake]

授权文档明确：fanout 可以绕过新任务 admission gate，**但不授予新权限**。已有 Ticket 的人工跟进由其 Worker 处理，Main 不能重新夺走输入。背景订阅不授权，也不触发恢复。[授权与输入归属][authorization-routing]

文章将这类直接投递称为 Pass-through，并提出两种上下文机制：Inline & Spill 在当前输入中放摘要和标识，全文留在来源中；Chain of Recall 用 Trigger、Thread、Worker Session 等 ID 逐步检索。[文章 §3][article] 这些机制需要稳定 ID 和可读取的入口，不能只靠 Prompt 提醒“自行回忆”。

### 2.4 5 秒 Shell：转入后台，不是杀死命令

原型把 Bash/Pwsh 的 `timeoutMs`、`maxTimeoutMs` 和 jobs 等待窗口设为 5000 ms。[配置][preset] 命令超过窗口后进入后台，**不是立即终止**；后台任务保留 Worker slot，完成后唤醒原 Session。[执行生命周期][execution]

相关限制包括：

- Turn 结束后，后台 Shell 有 30 分钟限制，超时终止并报告 waiting；子 Agent 可以继续。
- 人工交接释放 slot 并停止子 Agent。部分 review server 等后台进程可以保留，不再触发完成唤醒。
- 结束 Assignment 时终止 jobs、释放子 Agent；进程关闭时每个 Worker 至多 10 秒收尾。
- Worker 子 Agent 使用 standard preset，Shell 前台窗口为 60 秒。[执行生命周期][execution]

文章 §4 进一步提出异步 ReAct：`start()` 立即返回 Started，完成事件进入 Session，`stop()` 提供终止入口。文章称 DSH 已有异步任务机制但未默认启用，本轮未验证这项整体声明。[文章 §4][article]

对 Nika 而言，关键不是复制 `5000`，而是明确等待时如何接收 steer/stop、取消如何到达资源，以及跨 Turn handle 的 owner、完成投递、重复事件、重启和孤儿资源。

### 2.5 Watchdog：文章有设计，公开快照未找到实现

文章 §6 描述独立的外部 Watchdog Session，由定时消息驱动，检查基础设施故障和语义上的任务进展或偏航，并可能调整提示或规则。[文章 §6][article]

本轮检查了固定原型 tree，并在内存中读取官方 tarball 的 211 个 `.ts/.tsx/.md/.yml/.json` 文件。`watch.?dog` 搜索为零匹配，tree 中也没有同名模块。因此只能确认文章提出了设计，不能确认该快照包含公开实现。这一结果不排除其他名称或内部实现。

Nika 的进程健康检测、lease fencing 和资源清理可在 M6/M8 闭合；能判断业务偏航、修改规则或重启任务的 Watchdog Agent 是另一项能力。

## 3. 实验数字：是作者报告，不是 Nika 收益承诺

文章 §7 的第一组实验是**合成调度负载**：每次约 300 条群聊/GitHub CI 消息、约 20 个工作请求，coding 由只消耗时间的工具模拟。四种设计各运行 5 次，统计从请求到结果完成的延迟。[文章 §7][article]

| 设计 | p50 / p95，分钟 | 每次实验所有 Session 的 cache-read tokens |
|---|---:|---:|
| Super-agent | 12.4 / 15.7 | 23.2M |
| 去掉 Pass-through | 12.6 / 15.5 | 31.8M |
| 单 Session + subagent，中性 prompt | 20.4 / 29.5 | 6.1M |
| 单 Session + subagent，优先委派 prompt | 13.8 / 29.0 | 10.8M |

图注说明：消息间隔和工作耗时缩短到 1/4，模型调用时间没有缩放。因此，绝对分钟数依赖实验构造，不能直接当作真实开发任务的响应 SLA。[文章 §7 图注][article]

作者报告，中性 prompt 的单 Session 在 5/5 次运行中都未启动 subagent；优先委派版本在 4/5 次启动，另一次自行完成小任务并出现长尾。这说明 prompt 和分派策略会影响这组负载的排队行为，不能证明单 Session 架构必然更慢。[文章 §7][article]

去掉 Pass-through 后，Router 决策次数增加约 30%，cache-read 从 23.2M 增至 31.8M，按表值约增加 37%，延迟近似。**23.2M 是跨 Session 的缓存读取量，不是缓存容量、总 token、价格或 Nika 成本估计。**实验也没有评估代码质量。

另一组实验使用真实模型 Router 和 mock Workers，将触发频率从约 1 次/分钟提高到 40 次/分钟：平均批次 1.0 → 2.8，单请求时间 2.5 → 3.9 秒，摊销到每次决策的推理时间 6.4 → 1.6 秒。这是特定批处理实验，不能与第一组端到端时延混算。[文章 §7][article]

作者承认测试初步且简化。本轮没有独立可重跑的原始记录和 benchmark 程序，也没有重跑；固定原型文本中未找到 `12.4`、`15.7`、`23.2M`。证据等级是**一手实验报告，未经本轮独立复核**。

## 4. 默认权限：任务准入与进程访问分离

### 4.1 是否接受任务

原型的 Policy 定义来源 allowlist、addressed/unaddressed 处理和 `off / observe / auto` 模式。默认是 **observe**：评估候选任务并记录 would-accept 为 ignore，不启动工作。显式请求在三种模式中都可处理。未列出的来源默认 ignore；允许来源内的非点名消息可走 inferred 判断。[默认规则与模式][authorization-defaults]

Policy 更新不会立即终止运行中的 Ticket；恢复已结束任务时才重新验证 Mandate。[策略生效范围][authorization-update] 因此，当前 allowlist 不能说明所有存量执行已被撤权。

### 4.2 接受任务后，进程能做什么

原型创建 Router/Worker session 后直接设置 `permissionPresets.set(..., 'danger-full-access')`。[Session 创建源码][session-permissions]

部署文档说明：进程按操作系统用户身份执行，无额外审批。凭证 wrapper 是默认使用路径，**不是进程隔离**；绝对路径程序或操作者 home 仍可能可达，建议使用专用 OS 账号。[部署边界][deployment]

因此，Mandate/Policy 的任务范围不等于 OS 访问控制，部分约束由 Prompt 承担。本文未做完整安全审计，不能把 Mandate 描述成代码强制的最小权限沙箱。

也不能把原型的 full-access 推广到所有 DSH session。DSH 同时提供 `workspace-write + ask`、`danger-full-access + never` 等组合，默认值取决于配置。[DSH permission presets][dsh-permissions] DSH 也声明自己是未完成安全审计的开发预览；沙箱和审批降低风险，但不保证隔离。[DSH SAFETY][dsh-safety]

## 5. 对 Nika 的有限推论

当时 Nika 基线是统一的原生 AgentLoop、按入口持久 Session、按需执行、普通输入 steer、首版串行工具、单容器、无远程设备，不自动提升 Wiki/RSI，也不引入通用 EventBus 或任务卡。Proactive 的价值已接受，但触发、授权和打扰策略仍待设计。[讨论路线](../architecture/discussion-roadmap.md)、[AgentLoop](../design/agent-loop.md)、[接手文档](https://github.com/12bitsD/nika/blob/main/docs/architecture/development-handoff.md)

| 检查问题 | 对 Nika 的用法 |
|---|---|
| 哪条可信输入授权了什么？背景消息是否被当成授权？ | 补可信输入、当前有效范围和来源关联，不必先引入 Ticket/Mandate 第二套状态 |
| 消息是否重复，由谁处理，何时注入？ | 完善 SessionManager / Store 的事件身份、接受/注入关联与 owner，不从 Pass-through 推导另一条 task queue |
| 长工具退出、超时、取消、失联后，资源归谁？ | 补 ToolRuntime、lease、结果提交和孤儿资源处理；跨 Turn 后台 job 单独决定 |
| 用户怎样知道仍在做什么，何时需要回应？ | 从 Trajectory / SessionCatalog 导出只读观察，显示事实、更新时间和未知状态，不以模型自述作为权威 |
| 哪些变化值得主动打扰？ | 纳入 Proactive 专题，先定场景和注意力收益，再决定持续订阅、规则唤醒或 Router |
| 如何比较设计价值？ | 固定模型、工具、缓存和真实负载，比较成功率、重复副作用、错误启动、恢复、人工介入、时延和各类 token |

Router/Worker、持续订阅唤醒和语义 Watchdog 都会改变任务归属或生命周期，是否采用需要明确选择。文章 §5 的 state/compute/environment 分离可用于检查资源责任，但远程环境/BYOM 超出 Nika 当时的单容器基线。[文章 §5][article]

文章 §8 仍把 Router 分片、Worker 拓扑、月级记忆、无 Session 交互、A2A 隔离和凭证安全列为开放问题，并未证明已经解决。[文章 §8][article] 对 Nika 更有价值的是借它检查行为缺口，而不是复制系统外形。

## 6. 核验方法与未覆盖项

- 通过 GitHub API 固定两个官方仓库 SHA，读取 README、领域/授权/执行/架构文档和源码；通过官方 X 页面读取文章和发布元数据。英文页面由中文原稿机器翻译，细微术语应回看原稿。
- 原型 tarball 仅在内存中解析和搜索，没有解压安装或执行。搜索不到实现的结论只适用于记录的版本和范围。
- 用户提供的 Marktechpost 二手页面返回 HTTP 403，未作为事实依据。GitHub 检索只用于定位官方来源。
- 未验证真实部署、权限逃逸、飞书链路、任务成功率、成本账单或实验可重复性。公开版与内部实现的差异仍未知。
- ReAct、inbox、spill 和社区成本主张见[另一份证据笔记](2026-11-02-loop-evidence-notes.md)，本文不把社区报告升级为官方 benchmark。

[article]: https://x.com/DeepSeekHarness/status/2116857257245987236
[dsh-readme]: https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/README.md
[dsh-safety]: https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/SAFETY.md#L5-L23
[dsh-permissions]: https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/docs/subsystems/permission-presets.md#L9-L41
[prototype-readme]: https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/README.md#L3-L9
[domain]: https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/docs/001-domain.md#L7-L43
[orchestrator-prompt]: https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/prompts/orchestrator.md#L1-L16
[router-tools]: https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/docs/007-architecture.md#L490-L532
[tickets]: https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/src/core/tickets.ts#L32-L114
[intake]: https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/src/core/intake.ts#L19-L60
[authorization-routing]: https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/docs/002-authorization.md#L18-L42
[authorization-defaults]: https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/docs/002-authorization.md#L65-L101
[authorization-update]: https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/docs/002-authorization.md#L107-L123
[preset]: https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/cordis.patch.yml#L13-L144
[execution]: https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/docs/003-execution.md#L25-L47
[session-permissions]: https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/src/app/sessions.ts#L137-L154
[deployment]: https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/docs/006-deployment.md#L13-L19
