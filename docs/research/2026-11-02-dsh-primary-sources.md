# DSH / Super-agent：一手来源核验与 Nika 适用边界

核验日期：2026-11-02（Asia/Shanghai）。范围：用户综述涉及的 DeepSeek Harness、Super-agent 文章和公开研究原型。本文记录来源与分析，不改变 Nika 的架构或执行授权；未运行 DSH/原型，也未复现实验。

## 1. 先区分三种材料

| 材料 | 本轮固定版本 / 日期 | 能证明什么 |
|---|---|---|
| `deepseek-ai/deepseek-harness` | `ce7073d51254c26cd63b22a60835b8f948deb447`，提交于 2026-10-29 | DSH 基础 harness 的公开接口、实现和安全声明 |
| *Exploring Super-agent Design: Towards a More Autonomous Agent System* | DeepSeekHarness 官方文章，页面发布元数据为 2026-11-01 11:37:54 UTC | 作者的设计主张、内部实验报告与未解决问题；文章不可用 Git SHA 固定 |
| `deepseek-ai/dsh-superagent-prototype` | `6337fc823ce20251c8de8085948165c81aaa701e`，提交于 2026-10-31 | 基于 DSH 的公开应用原型；不等于基础 harness，也不等于文章中全部设计 |

DSH 自称以 Cordis 为基础、Everything is a Plugin 的开发者预览，接口可能发生兼容性变化。[DSH README][dsh-readme] Super-agent 原型则明确声明：**研究参考，不是 DeepSeek 正式产品；大量工程由 coding agents 完成，尚不适合生产；公开版经改编，与内部实现及文章不完全一致，部分设计尚未实现。** 其目标是围绕一个 GitHub 仓库开展主动 coding，可选接入飞书。[原型 README][prototype-readme]

文章开头也明确说，它讨论架构思想，不是介绍现有 DSH 功能。下文的“文章提出”“源码存在”和“作者报告实验”不能相互替换。[文章导言][article]

## 2. 机制核验

### 2.1 Router / Worker：文章中的角色与原型的限制不同

文章 §2 提出一个长期 Router Session，积累跨入口上下文、识别新任务并协调独立 Workers；Router 不承担实际任务执行，不是每步规划与拆解的传统 Team Lead。Worker 按任务隔离上下文、自己规划执行，必要时再启动子 Agent。外部沟通经 Router 汇总；多个 Worker 通过订阅共享来源上下文，不等于任意点对点互发消息。模型推理期间到达的 Router 输入可以批量进入下一轮。[文章 §2–3][article]

公开原型确有 Main Session / Executor Session、Ticket、Assignment 等角色。Router 的 prompt 要求把调查和实现交给 Worker、不做实现计划、不用 sleep/polling 等待。[领域模型][domain]、[Router prompt][orchestrator-prompt] **但“Router 不读代码”是 prompt 规则，工具隔离没有强制实现：**原型架构说明仍给 Router 提供 DSH 的 Bash、read、read_image、jobs 等工具。[Router 工具边界][router-tools]

因此，文章中的“Router 可以没有 Bash”不能被转述为“公开原型 Router 已无执行权限”，也不能据角色命名推断安全边界。

### 2.2 Ticket / Mandate：授权来源与执行身份分开

原型的领域链路是 `Trigger → Eval → Ticket / Mandate → Assignment / Executor → Channel`：[领域定义][domain]

- Trigger 保存不可变、可去重的外部事实；Thread 组织共同上下文。
- Ticket 是工作身份，独立于某次执行是否仍活跃；Assignment 绑定具体执行实例。
- Mandate 保存授权出处：Policy 版本、范围、授权强度、命中规则、actor 与接收方式等。超出范围需要新 Ticket；已结束 Ticket 续跑前重新验证授权。
- Subscription 可以让多个 Ticket 关注同一 Thread，传递上下文不产生新授权。

这不只是文档名词：`createTicket` 写入 Ticket/Mandate、订阅起始 Threads、确认后启动 executor；attach 拒绝人工停止的 Ticket，并对已结束 Ticket 的 mandate 重新验证。[Ticket 创建与续接][tickets]

**可借鉴的是可追溯授权和执行身份分离。** Nika 是否需要独立 Mandate 对象仍属设计选择；不可把“授权出处不可变”理解成“用户不能用后续 steer 修改目标”。新的可信用户输入如何更新有效授权，必须由自己的协议解释。

### 2.3 Subscription / Pass-through：省去重复路由，不放大权限

原型 intake 先去重 Trigger、检查来源 scope，再为订阅者记录输入并唤醒；仅当没有 Ticket 拥有该输入时才进入新 Eval。context-only 输入不恢复已停止执行；人工停止的 Ticket 不会被普通订阅消息自动复活。[intake 实现][intake]

授权文档明确：fanout 可绕过新任务 admission gate，**但不授予新权限**。已有 Ticket 内的正常人工跟进由其 Worker 处理，Main 收到通知但不能重新夺走这条输入；越界输入交回，在没有 Ticket 拥有它时才能产生新 Eval。仅作为背景订阅的 Thread 不授予授权，也不触发恢复。[授权与输入归属][authorization-routing]

文章把这类直接投递称为 Pass-through，并配合两类上下文机制：Inline & Spill 把必要摘要/标识放入当前输入，把全文留在可读取的来源；Chain of Recall 用 Trigger、Thread、Worker Session 等 ID 串联逐步检索。[文章 §3][article] 这些机制需要稳定身份和可用读入口，不能只在 prompt 中写“自行回忆”。

### 2.4 5 秒 Shell：让出前台，不是 5 秒后杀命令

原型的 Router/Worker preset 将 Bash/Pwsh 的 `timeoutMs`、`maxTimeoutMs` 和 jobs 等待窗口设为 5000 ms。[配置][preset] 执行文档明确：命令超过窗口进入后台，**不是立即终止进程**；后台任务保留 Worker slot，完成后唤醒原 Session。[执行生命周期][execution]

对应的生命周期约束包括：Turn 结束后，后台 Shell 有 30 分钟限制，超时会被终止并报告 waiting；子 Agent 可以继续。人工交接会释放 slot 并停止子 Agent；部分 review server 等 Shell 后台进程可保留且不再触发完成唤醒。结束 Assignment 时终止 jobs、释放子 Agent；关闭进程时为每个 Worker 留至多 10 秒收尾。Worker 子 Agent 使用 standard preset，Shell 前台窗口为 60 秒。[执行生命周期][execution]

文章 §4 进一步提出通用异步 ReAct：`start()` 立即返回 Started，完成通过事件进入 Session，`stop()` 提供终止入口；文章称 DSH 已实现异步任务机制，但尚未默认启用。[文章 §4][article] 本轮未运行验证这项整体声明，也不把公开原型的 Shell 配置当成所有工具的通用后台协议。

**对 Nika 的直接问题是：**长工具能否在等待时接纳 steer/stop，以及取消如何传到实际资源；若要返回 handle 后跨 Turn 继续，则还要明确 owner、完成投递、重复事件、重启与孤儿资源。不能仅复制 `5000` 这个数值。

### 2.5 Watchdog：文章有设计，检查到的公开原型未找到实现

文章 §6 描述一个独立、外部 Watchdog Session，由定时消息驱动，同时检查基础设施故障和语义上的任务进展/偏航，并可能调整提示或触发规则。[文章 §6][article]

本轮检查固定原型 tree，并在内存中读取官方 tarball 的 211 个 `.ts/.tsx/.md/.yml/.json` 文件；对 `watch.?dog` 的搜索为零匹配，tree 中也没有同名模块。**可确认文章提出该设计；未能确认该快照包含对应公开实现。** 这一负面结果限于所检查的路径与文本，不证明不存在其他名称的相关机制或内部实现。

Nika 的进程健康检测、lease fencing、资源清理可在 M6/M8 闭合；会判断目标偏航、改规则或重新触发业务执行的 Watchdog Agent 是另一项能力，不能由健康检测自然推导出来。

## 3. 实验数字：可确认报告存在，不能当成 Nika 预期收益

文章 §7 的第一组实验是**合成调度/协调负载**：每次约 300 条群聊/GitHub CI 消息，约 20 个工作请求，其余包括闲聊、跟进、取消和 CI 消息；实际 coding 由只消耗时间的工具模拟。四种设计各运行 5 次，统计从工作请求到结果消息完成的延迟。[文章 §7][article]

| 设计 | p50 / p95，分钟 | 每次实验所有 Session 的 cache-read tokens |
|---|---:|---:|
| Super-agent | 12.4 / 15.7 | 23.2M |
| Super-agent 去掉 Pass-through | 12.6 / 15.5 | 31.8M |
| 单 Session + subagent，中性 prompt | 20.4 / 29.5 | 6.1M |
| 单 Session + subagent，优先委派 prompt | 13.8 / 29.0 | 10.8M |

文章的图注说明：**消息间隔和工作耗时缩短到 1/4，模型调用时间没有缩放。** 因而这些绝对分钟数依赖该实验构造，不能直接解释为真实开发任务的响应 SLA。[文章 §7 图注][article]

作者报告，中性 prompt 的单 Session 在 5/5 次运行均未启动 subagent；优先委派版本在 4/5 次启动，另一次选择自行完成小任务而出现长尾。这支持“prompt 与分派策略会影响这组负载的排队行为”，不能证明所有单 Session 架构天然更慢。[文章 §7][article]

去掉 Pass-through 后，Router 的决策次数增加约 30%，cache-read 从 23.2M 到 31.8M，按表值计算增加约 37%；延迟近似。**23.2M 是跨 Session 的缓存读取 token 量，不是缓存容量、总 token、价格或 Nika 成本估计。** 实验也没有评估代码质量，不能把节约重复路由解释成任务正确率提高。

另一组 Router 压力实验使用真实模型 Router 和 mock Workers，把需要决策的触发频率从约 1 次/分钟提高到 40 次/分钟：平均批次 1.0 → 2.8，单请求时间 2.5 → 3.9 秒，摊销到每次决策的推理时间 6.4 → 1.6 秒。这是作者报告的特定批处理实验，不能与前一组端到端任务时延混算。[文章 §7][article]

作者明确承认测试初步、简化且有些部分可以更严谨。本轮没有获得可独立重跑的原始运行记录与 benchmark 程序，也没有重跑；固定原型所检查文本中搜索 `12.4`、`15.7`、`23.2M` 未命中。结论的证据等级是**一手实验报告，尚未经本轮独立复核**。

## 4. 默认权限：任务 admission 与进程访问必须分开

### 4.1 是否接受一个任务

公开原型的 Policy 定义了来源 allowlist、addressed/unaddressed 处理及 `off / observe / auto` 模式。默认是 **observe**：对推断出来的候选任务进行评估，把 would-accept 记为 ignore，不启动工作；显式请求在三种模式中均可处理。未列出的 addressed 与 unaddressed 来源默认 ignore；允许来源内的非点名消息可走 inferred 判断。[默认规则与模式][authorization-defaults]

Policy 更新不会立即终止正在运行的 Ticket；恢复已结束任务时重新验证 Mandate。[策略生效范围][authorization-update] 因而不能只看一个当前 allowlist 就推断所有存量执行已被撤权。

### 4.2 被接受后，进程能做什么

原型 `ensure(...)` 创建 Router/Worker session 后直接调用 `permissionPresets.set(..., 'danger-full-access')`。[Session 创建源码][session-permissions] 部署文档也明确：二者按操作系统用户的身份执行，无额外审批；凭证 wrapper 是默认使用路径，**不是进程隔离**，绝对路径程序或操作者 home 仍可能可达，建议使用专用 OS 账号。[部署边界][deployment]

这意味着 Mandate/Policy 的任务范围与 OS 访问控制并不等价；部分业务约束由 prompt 承担。本文未开展完整安全审计，也不能把“有 Mandate”写成“代码强制的最小权限沙箱”。

另一方面，**不能把原型强制 full-access 推广到所有 DSH session**。DSH permission-presets 文档同时提供 `workspace-write + ask`、`danger-full-access + never` 等组合，新 Session 的默认值取决于配置或 `defaultPreset`。[DSH permission presets][dsh-permissions] DSH 的安全声明亦说明它是未完成安全审计的开发预览，可以执行模型生成的命令；沙箱和审批降低风险但不保证隔离。[DSH SAFETY][dsh-safety]

## 5. 对 Nika 的有限推论

当前 Nika 基线是统一的原生 AgentLoop、按入口持久 Session、按需执行、普通输入 steer、首版串行工具；一个容器，不接远程设备，不自动提升 Wiki/RSI，不引入通用 EventBus 或任务卡。Proactive 的价值已接受，但触发、授权和打扰策略尚待专题设计。[讨论路线](../architecture/discussion-roadmap.md)、[AgentLoop](../design/agent-loop.md)、[接手文档](https://github.com/12bitsD/nika/blob/main/docs/architecture/development-handoff.md)

| 可用的检查问题 | Nika 当前应如何使用 |
|---|---|
| 哪条可信输入授权了什么？背景消息是否被当成授权？ | 补可信输入、当前有效范围和来源关联；不必先创建 Ticket/Mandate 第二套状态对象 |
| 消息是否重复，当前由谁处理，何时注入？ | 完善 SessionManager / Store 的事件身份、接受/注入关联与 owner；不从 Pass-through 推导另一条 task queue |
| 长工具退出、超时、取消、进程失联后，资源归谁？ | 补 ToolRuntime、lease、结果提交与孤儿资源处理；后台跨 Turn job 作为单独行为决定 |
| 用户怎样知道还在做什么，以及何时需要回应？ | 只读观察从 Trajectory / SessionCatalog 导出；把事实、更新时间和未知状态显式呈现，不让模型自述充当权威状态 |
| 哪些变化值得主动打扰，哪些只需内部整理？ | 纳入 Proactive 专题，先定场景和注意力收益，再决定是否需要持续订阅、规则唤醒或 Router |
| 如何比较设计的实际价值？ | 固定模型、工具、缓存条件和真实负载，测任务成功、重复副作用、错误启动、恢复、人工介入/打扰、时延与各 token 类别 |

Router/Worker、持续订阅唤醒、语义 Watchdog 都需要改变任务归属或生命周期协议；是否采用须明确选择，不能因原型存在就成为 Nika 必做模块。文章 §5 的 state/compute/environment 分离可以帮助检查资源责任，但其远程环境/BYOM 设想超出 Nika 的单容器、无远程设备基线。[文章 §5][article]

文章 §8 仍把 Router 分片、Worker 协作拓扑、月级记忆、无 Session 产品交互、A2A 隔离和凭证安全列作开放问题。它没有证明这些问题已解决。[文章 §8][article] 对 Nika 更有价值的使用方式是借它检查现有模块的行为缺口，而不是把其系统外形当作验收目标。

## 6. 核验方法与未覆盖项

- 通过 GitHub API 固定两个官方仓库 SHA，读取 README、领域/授权/执行/架构文档及相关源码；通过官方 X 页面直接读取文章正文与发布元数据。英文页面说明它由中文原稿机器翻译，涉及细微术语差异时应回看原稿。
- 对原型官方 tarball 仅在内存中解析、搜索文本；没有解压安装或执行任何仓库程序。搜索不到实现的结论只限于记录的版本与范围。
- 用户给出的 Marktechpost 二手页面返回 HTTP 403；未用其作为事实依据。GitHub 检索仅用于定位官方来源。
- 未验证真实部署、权限逃逸、飞书链路、任务成功率、成本账单或实验可重复性。文章公开版与内部实现的差异仍未知。
- 相关但不同的 ReAct、inbox、spill 和社区成本主张核验见[另一份证据笔记](2026-11-02-loop-evidence-notes.md)；本文件不重复把社区报告升级为官方 benchmark。

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
