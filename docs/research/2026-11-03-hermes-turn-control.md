# Hermes：Turn 结束、steer 与 stop 的源码证据

核验：2026-11-03。对象为 **NousResearch/hermes-agent**；官方 README 明确称 Hermes Agent 由 Nous Research 构建，本次未发现 Nika 引用另一同名项目的坐标。[项目身份][identity] 本轮读取时官方 `main` 固定到 **`d7572ceda754b888216c212691e1b2301ebe0359`**，提交时间 `2026-11-03T05:12:33Z`。[版本][commit]

证据范围：GitHub API 与固定 SHA 的公开源码、测试文本；**未安装、执行代码或运行测试**。下文“实现”表示源码存在，“测试”表示有该用例，均不代表本轮实测通过。重点是原生 Python loop 与所检查的 gateway 路径，不涵盖 Codex app-server 等所有运行模式。

## 1. 无 Tool Call 是结束候选，不直接等于任务成功

原生 driver 先规范化模型响应，再按 `assistant_message.tool_calls` 分派 `run_tool_round` 或 `finish_text_response`；分支可返回 `continue / break / return`。正常 break 后调用 `finalize_turn`。[driver][driver]

`finish_text_response` 先处理协议与输出形态：空内容/仅 think block 进入恢复；短回复尾部声称还要行动、做过工具后却只留碎片、部分 acknowledgment 命中有条件的 heuristic 时，共用最多两次 continuation 提醒。它们是有作用范围的规则，不是对任意“纯承诺”或业务成功的可靠分类。[输出形态检查][text-shape] Provider 声称 `finish_reason="tool_calls"` 却给空数组时，还会有限次重新请求。正常文本进入 stop gates；通过后进行输出变换、追加 assistant row、尝试 flush，最后返回 break。flush 失败会记录 warning 并依赖 finalizer 重试，**不是本轮已证实的原子提交**。[文本出口][text-exit]

当前 stop gates 是具体检查：迟到的 LSP 结果、verify-on-stop、`pre_verify` hook，以及 Kanban Worker 终态工具约束；命中时保留候选回答并增加一条继续处理的输入。[stop gates][stop-gates] 这些是 Hermes 的扩展行为，不构成通用业务验收证明，也不意味着 Nika 应复制验证插件、Kanban 或其预算策略。

**与当前问题最直接的事实：所检查的文本出口及 stop gates 没有把“steer buffer 必须为空”作为正常 final 的统一前置条件。** 没来得及注入的 steer 有另一条交回外层的路径，见下节。这是对这些具体路径的源码判断，不是所有模式不存在竞态的保证。

## 2. steer、redirect、下一 Turn 分别处理

### 三种操作并不相同

| 操作 | 所检查实现的行为 | 证据 |
|---|---|---|
| `steer(text, ticket=...)` | 不中断当前工作；在锁内追加到 buffer，多次输入按该锁内追加顺序保留，可按 ticket 标识一次提交 | [steer][steer] |
| `redirect(text)` | 模型请求活跃时只取消该请求并在当前 Turn 重建；工具执行时降级为 steer，支持的终端可被请求让出前台；若模型已经完成，返回 False 供外层安排下一 Turn | [redirect][redirect] |
| gateway 普通忙时输入 | 根据 busy mode 选择 steer/redirect/queue；成功注入的文本不再同时排队，未成功的进入后续 Turn；内部合成事件单独排队 | [gateway 路由][busy-routing] |

`steer()` 返回 True 只证明该方法接受了非空文本并写入内存 buffer；它本身没有“已进入模型请求”或“已持久化”的保证。并发追加使用锁，不能据此推广为跨进程的严格全局消息顺序或 durable inbox。[steer][steer]

### 什么时候注入，什么时候留到下一 Turn

1. Tool batch 结束时 drain steer，追加独立 `role=user` row，保留已存在的 Tool Result 原文。若该批没有可用 Tool Result，则放回 buffer。该 row 后续通过正常 flush 进入存储。[批次边界][batch-inject]
2. 下一次模型调用准备阶段再 drain 一次，以接纳上次模型请求期间到达的 steer；实现寻找最近 Tool Result，并在其后插入 user row。没有 Tool Result 时放回 buffer。[调用前边界][pre-api]
3. 最终回答后，finalizer drain **无 ticket** 的 leftover steer，经返回值 `pending_steer` 交给调用层作为下一 Turn；有 ticket 的提交仍留在 buffer，由 gateway transfer 的 settlement 按原身份处理，避免同时成为重复 follow-up。[finalizer handoff][leftover]、[ticket drain][ticket-drain]
4. gateway 的 `_merge_leftover_steer` 明确处理它与排队事件、interrupt replacement 的相对位置；对应测试覆盖独立 steer、内部事件、已有队列和 interrupt 四类情形，并断言消息及 channel pins 保留。[gateway merge][merge]、[对应测试][merge-test]

因此，Hermes 提供的是**当前 Turn 的注入机会 + 未消费输入的移交责任**。它没有要求每条已接纳输入都必须由当前 final 回答；也不能把这些局部操作描述成一个覆盖“接纳—final commit—下一 Turn 启动”的统一事务。

### 结束前后并发顺序不是只检查一次 pending

gateway 在交接下一条输入时清除同一个 session guard 的 Event 状态，而非先删除 guard；清理中的 await 结束后，再运行 `_finish_session_task` 检查迟到消息：已有新 owner 则重新放回 pending，否则生成 drain task；没有 pending 时才按 owner 清理。[交接与 finally][gateway-finally]、[最终 owner 协调][gateway-owner]

对应回归测试人为暂停 typing cleanup，检查 guard 仍是同一对象；另一个测试在 finally 的 await 中插入 late message，断言它被处理而非丢弃。[并发回归测试][race-tests] 这是进程内 asyncio 任务间的保护及测试覆盖，不证明 crash recovery、持久 lease 或恰好一次交付。

redirect 的结束竞态还有较窄的保护：在 redirect 锁内重查模型 active 标志，已经完成就拒绝 correction；hard stop 与 redirect 共用该锁，防止 stop 被变成一次 retry。[redirect][redirect]、[stop 仲裁][interrupt] 相应测试包含“模型在拿锁前完成”和“hard stop 与 redirect 并发”。[redirect 测试][redirect-tests]

## 3. stop、部分结果与清理的实际限度

`interrupt()` 标记原因和取消状态，尝试中止模型请求，向本 Agent 的 execution/tool worker threads 及子 Agent 传播取消。它是取消请求，不是所有资源都已停止的证明。[取消传播][interrupt]

顺序执行器在每个工具前检查取消；当前工具已返回的结果正常发布，尚未启动的调用补出有对应身份的 skipped/cancelled Tool Result，从而保留调用与结果配对。[顺序停止][sequential-stop] 对正在运行、拒不响应的工具，它按窗口轮询，取消后给 3 秒合作退出机会；若仍未结束，合成 cancelled outcome 并放弃等待，`executor.shutdown(wait=False, ...)` 不 join 卡住的 worker。[放弃等待][abandon]

**这里必须保留未知：线程被放弃不代表其副作用已中止或回滚。** 对应测试用一个睡眠 30 秒的不合作假工具，要求执行器提前返回 cancelled；另一个测试要求合作工具在 grace 内返回时优先保留真实结果。测试目标是等待可以结束，不是证明任意后台动作都已终止。[工具取消测试][tool-cancel-tests]

finalizer 分别保护 save trajectory、资源清理和会话持久化步骤，某一步抛错不跳过后续步骤，错误可放入 `cleanup_errors`，仍返回已有回答。[清理顺序][finalizer-cleanup] 资源 helper 明确跳过持久 terminal env 和 headed browser 的逐 Turn 销毁，交给 inactivity reaper；其内部清理异常也可能只被日志处理。[资源 helper][resources] 因而“finalizer 返回”不等于“所有资源销毁成功”。对应测试主要验证清理错误不吞掉响应和后续步骤，未在本轮执行。[清理测试][cleanup-test]

另一个与 Nika 不同的选择是：Hermes 的 `clear_interrupt(hard_cancel=True)` 会丢弃未消费 steer；soft clear 则保留，供继续注入或移交。源码与测试明确区分两者。[hard/soft clear][clear] Nika 当前约定 `/stop` 后保留未注入 steer，应保持自己已确认的语义，而非照搬此项。

## 4. 当前 Nika 只需要借此收敛三个问题

以下为分析建议，未改动 Nika 决定。

1. **final 的边界在哪里？** 模型给出无 Tool Call 的回答只产生结束候选；应明确在 final commit 前接纳的 steer 是否还进入当前 Turn，以及哪个同步边界作出决定。commit 后的输入进入下一 Turn。Hermes 的 leftover handoff 说明“允许结束”仍必须交代未消费输入的归宿；不能只写 `if inbox empty then finish` 而省略并发接纳。
2. **移交期间谁拥有输入和执行权？** Nika 已有 Session-owned buffer、accepted/injected 区分和单 writer。需要用可验证的事件/事务协议连接它们：一条输入只属于当前模型请求或未来 Turn；finalization、投递与新 Turn 交接期间都有 owner，旧回调不能抢回写权限。Hermes 的 guard/late-drain 回归可作为测试场景，不能充当 Nika 的持久性证明。
3. **停止完成到底证明了什么？** 分开表示“已发取消”“执行已退出”“结果/副作用未知”“清理仍待处理”；补每个未结束 Tool Call 的关联结果，并把资源 owner 与迟到结果隔离。保留 Nika 的未知状态及 pending steer 规则，不采用“合成 cancelled 就视为彻底停止”。

建议最小验证矩阵：steer 在模型返回前、返回后但 final commit 前、commit 后、清理 await 中分别到达；stop 与 steer 并发；Tool A 完成而 Tool B 正在运行时 stop；工具不响应取消；持久化或清理失败。分别检查输入归属、调用/结果配对、下一请求内容、writer 交接与用户可见结束语义。无需由这些材料引入 Router、额外 task queue 或硬调用预算。

局限：本轮未遍历所有入口、进程间协议、存储迁移和重启恢复。补充抓取曾遇到 GitHub Raw HTTP 429，必要片段改经官方 Contents API 取得；本文仅引用已成功读到的范围。

[identity]: https://github.com/NousResearch/hermes-agent/blob/d7572ceda754b888216c212691e1b2301ebe0359/README.md#L5-L25
[commit]: https://github.com/NousResearch/hermes-agent/commit/d7572ceda754b888216c212691e1b2301ebe0359
[driver]: https://github.com/NousResearch/hermes-agent/blob/d7572ceda754b888216c212691e1b2301ebe0359/agent/conversation_loop.py#L1745-L1799
[text-shape]: https://github.com/NousResearch/hermes-agent/blob/d7572ceda754b888216c212691e1b2301ebe0359/agent/turn_final_response.py#L179-L298
[text-exit]: https://github.com/NousResearch/hermes-agent/blob/d7572ceda754b888216c212691e1b2301ebe0359/agent/turn_final_response.py#L332-L420
[stop-gates]: https://github.com/NousResearch/hermes-agent/blob/d7572ceda754b888216c212691e1b2301ebe0359/agent/turn_stop_gates.py#L134-L223
[steer]: https://github.com/NousResearch/hermes-agent/blob/d7572ceda754b888216c212691e1b2301ebe0359/agent/interrupt_control.py#L310-L321
[redirect]: https://github.com/NousResearch/hermes-agent/blob/d7572ceda754b888216c212691e1b2301ebe0359/agent/interrupt_control.py#L323-L384
[busy-routing]: https://github.com/NousResearch/hermes-agent/blob/d7572ceda754b888216c212691e1b2301ebe0359/gateway/run_busy.py#L930-L1015
[batch-inject]: https://github.com/NousResearch/hermes-agent/blob/d7572ceda754b888216c212691e1b2301ebe0359/agent/agent_runtime_helpers.py#L3662-L3705
[pre-api]: https://github.com/NousResearch/hermes-agent/blob/d7572ceda754b888216c212691e1b2301ebe0359/agent/turn_iteration_prep.py#L175-L295
[leftover]: https://github.com/NousResearch/hermes-agent/blob/d7572ceda754b888216c212691e1b2301ebe0359/agent/turn_finalizer.py#L841-L854
[ticket-drain]: https://github.com/NousResearch/hermes-agent/blob/d7572ceda754b888216c212691e1b2301ebe0359/agent/interrupt_control.py#L398-L432
[merge]: https://github.com/NousResearch/hermes-agent/blob/d7572ceda754b888216c212691e1b2301ebe0359/gateway/run_busy.py#L207-L257
[merge-test]: https://github.com/NousResearch/hermes-agent/blob/d7572ceda754b888216c212691e1b2301ebe0359/tests/gateway/test_pending_steer_drain.py#L35-L82
[gateway-finally]: https://github.com/NousResearch/hermes-agent/blob/d7572ceda754b888216c212691e1b2301ebe0359/gateway/platforms/base.py#L4549-L4585
[gateway-owner]: https://github.com/NousResearch/hermes-agent/blob/d7572ceda754b888216c212691e1b2301ebe0359/gateway/platforms/base.py#L4438-L4460
[race-tests]: https://github.com/NousResearch/hermes-agent/blob/d7572ceda754b888216c212691e1b2301ebe0359/tests/gateway/test_pending_drain_race.py#L73-L200
[interrupt]: https://github.com/NousResearch/hermes-agent/blob/d7572ceda754b888216c212691e1b2301ebe0359/agent/interrupt_control.py#L163-L270
[redirect-tests]: https://github.com/NousResearch/hermes-agent/blob/d7572ceda754b888216c212691e1b2301ebe0359/tests/agent/test_steer.py#L182-L231
[sequential-stop]: https://github.com/NousResearch/hermes-agent/blob/d7572ceda754b888216c212691e1b2301ebe0359/agent/tool_executor.py#L1870-L1924
[abandon]: https://github.com/NousResearch/hermes-agent/blob/d7572ceda754b888216c212691e1b2301ebe0359/agent/tool_executor.py#L866-L994
[tool-cancel-tests]: https://github.com/NousResearch/hermes-agent/blob/d7572ceda754b888216c212691e1b2301ebe0359/tests/agent/test_sequential_tool_interrupt.py#L52-L129
[finalizer-cleanup]: https://github.com/NousResearch/hermes-agent/blob/d7572ceda754b888216c212691e1b2301ebe0359/agent/turn_finalizer.py#L697-L736
[resources]: https://github.com/NousResearch/hermes-agent/blob/d7572ceda754b888216c212691e1b2301ebe0359/agent/chat_completion_helpers.py#L2334-L2354
[cleanup-test]: https://github.com/NousResearch/hermes-agent/blob/d7572ceda754b888216c212691e1b2301ebe0359/tests/agent/test_turn_finalizer_cleanup_guard.py#L148-L180
[clear]: https://github.com/NousResearch/hermes-agent/blob/d7572ceda754b888216c212691e1b2301ebe0359/agent/interrupt_control.py#L278-L308
