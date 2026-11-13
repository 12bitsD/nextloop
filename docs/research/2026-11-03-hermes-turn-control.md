# Hermes：Turn 结束、steer 与 stop 的源码证据

核验日期：2026-11-03。对象：**NousResearch/hermes-agent**，固定 `main` 为 **`d7572ceda754b888216c212691e1b2301ebe0359`**，提交时间 `2026-11-03T05:12:33Z`。[项目身份][identity]、[版本][commit]

证据来自固定 SHA 的公开源码和测试文本，**本轮未安装、执行代码或运行测试**。“实现”表示源码存在，“测试”表示存在用例，不表示本轮实测通过。重点是原生 Python loop 和所检查的 gateway 路径，不覆盖 Codex app-server 等所有模式。

## 1. 无 Tool Call 只是结束候选

原生 driver 规范化模型响应后，根据 `assistant_message.tool_calls` 选择 `run_tool_round` 或 `finish_text_response`。分支可返回 `continue / break / return`，正常 break 后调用 `finalize_turn`。[driver][driver]

`finish_text_response` 会先检查输出：空内容或仅 think block 进入恢复；短回复尾部声称还要行动、工具后只剩碎片、部分 acknowledgment 命中特定 heuristic 时，最多进行两次 continuation 提醒。这些规则有特定作用范围，不能可靠判断任意“纯承诺”或业务成功。[输出形态检查][text-shape]

Provider 声称 `finish_reason="tool_calls"` 却返回空数组时，也会有限次重新请求。正常文本通过 stop gates 后进行输出变换、追加 assistant row 并尝试 flush，最后返回 break。flush 失败只记录 warning 并交给 finalizer 重试，**不是本轮已证实的原子提交**。[文本出口][text-exit]

Stop gates 检查迟到的 LSP 结果、verify-on-stop、`pre_verify` hook 和 Kanban Worker 终态工具约束。命中时保留候选回答并追加继续处理的输入。[stop gates][stop-gates] 这些是 Hermes 的扩展行为，不是通用业务验收，也不意味着 Nika 应复制其插件、Kanban 或预算策略。

**所检查的文本出口和 stop gates，没有把“steer buffer 必须为空”作为正常 final 的统一前置条件。** 未注入的 steer 有另一条移交路径，见下节。这只是对特定源码路径的判断，不是所有模式都没有竞态。

## 2. steer、redirect 和下一 Turn

### 三种操作不同

| 操作 | 所检查实现的行为 | 证据 |
|---|---|---|
| `steer(text, ticket=...)` | 不中断当前工作；在锁内追加到 buffer，多次输入保留锁内追加顺序，可按 ticket 标识一次提交 | [steer][steer] |
| `redirect(text)` | 模型请求活跃时取消该请求并在当前 Turn 重建；工具执行时降级为 steer；支持的终端可让出前台；模型已完成时返回 False | [redirect][redirect] |
| gateway 普通忙时输入 | 根据 busy mode 选择 steer/redirect/queue；成功注入的文本不再同时排队，未成功的进入后续 Turn；内部合成事件单独排队 | [gateway 路由][busy-routing] |

`steer()` 返回 True 只说明非空文本已写入内存 buffer，不证明已进入模型请求或已经持久化。锁也不能推出跨进程全局顺序或 durable inbox。[steer][steer]

### 注入与移交边界

1. Tool batch 结束时 drain steer，追加独立 `role=user` row，保留已有 Tool Result。若该批没有可用 Tool Result，则放回 buffer。该 row 后续通过正常 flush 存储。[批次边界][batch-inject]
2. 下一次模型调用准备阶段再次 drain，接纳上次请求期间到达的 steer。实现寻找最近的 Tool Result 并在其后插入 user row；没有 Tool Result 时放回 buffer。[调用前边界][pre-api]
3. 最终回答后，finalizer drain **无 ticket** 的 leftover steer，通过 `pending_steer` 交给调用层作为下一 Turn。有 ticket 的提交留在 buffer，由 gateway settlement 按原身份处理，避免重复 follow-up。[finalizer handoff][leftover]、[ticket drain][ticket-drain]
4. gateway 的 `_merge_leftover_steer` 处理它与排队事件、interrupt replacement 的相对位置。测试覆盖独立 steer、内部事件、已有队列和 interrupt，并检查消息及 channel pins 保留。[gateway merge][merge]、[对应测试][merge-test]

因此，Hermes 提供的是**当前 Turn 的注入机会，以及未消费输入的移交责任**。它不要求当前 final 回答每条已接纳输入，也没有覆盖“接纳—final commit—下一 Turn”的统一事务。

### 结束前后还有并发协调

gateway 在交接下一条输入时清除同一个 session guard 的 Event 状态，而不是先删除 guard。清理中的 await 结束后，`_finish_session_task` 再检查迟到消息：已有新 owner 则重新放回 pending，否则生成 drain task；没有 pending 时才按 owner 清理。[交接与 finally][gateway-finally]、[最终 owner 协调][gateway-owner]

回归测试人为暂停 typing cleanup，检查 guard 仍是同一对象；另一个测试在 finally 的 await 中加入 late message，断言它被处理而非丢弃。[并发回归测试][race-tests] 这些只证明进程内 asyncio 任务协调，不证明 crash recovery、持久 lease 或恰好一次交付。

redirect 还在锁内重查模型 active 标志；模型已经完成就拒绝 correction。hard stop 与 redirect 共用该锁，防止 stop 被变成 retry。[redirect][redirect]、[stop 仲裁][interrupt] 测试覆盖“拿锁前完成”和“hard stop 与 redirect 并发”。[redirect 测试][redirect-tests]

## 3. stop、部分结果与清理的限制

`interrupt()` 记录原因和取消状态，尝试中止模型请求，并向 Agent 的 execution/tool worker threads 和子 Agent 传播取消。它只是取消请求，不证明所有资源已经停止。[取消传播][interrupt]

顺序执行器在每个工具前检查取消：已返回的结果正常发布，未启动的调用补出有对应身份的 skipped/cancelled Tool Result，保持调用与结果配对。[顺序停止][sequential-stop]

对不响应的工具，执行器按窗口轮询，取消后给 3 秒合作退出机会；仍未结束时合成 cancelled outcome 并放弃等待。`executor.shutdown(wait=False, ...)` 不会 join 卡住的 worker。[放弃等待][abandon]

**线程被放弃不代表副作用已经中止或回滚。** 测试使用一个睡眠 30 秒的不合作假工具，要求执行器提前返回 cancelled；另一个测试确认合作工具在 grace 内返回时优先保留真实结果。测试证明等待可以结束，不证明后台动作已经终止。[工具取消测试][tool-cancel-tests]

finalizer 分别保护 save trajectory、资源清理和会话持久化。一步抛错不跳过后续步骤，错误进入 `cleanup_errors`，仍返回已有回答。[清理顺序][finalizer-cleanup]

资源 helper 跳过持久 terminal env 和 headed browser 的逐 Turn 销毁，交给 inactivity reaper；内部清理异常也可能只写日志。[资源 helper][resources] 因此，finalizer 返回不等于所有资源销毁成功。相关测试主要检查清理错误不会吞掉响应和后续步骤，本轮未执行。[清理测试][cleanup-test]

另一个差异是：Hermes 的 `clear_interrupt(hard_cancel=True)` 丢弃未消费 steer，soft clear 则保留。[hard/soft clear][clear] Nika 当前约定 `/stop` 后保留未注入 steer，不应照搬这一项。

## 4. 对 Nika 的三个检查问题

以下是分析建议，未改变 Nika 的决定。

1. **final 的边界在哪里？** 无 Tool Call 只产生结束候选。需要明确 final commit 前到达的 steer 是否进入当前 Turn，以及由哪个同步边界决定。commit 后的输入进入下一 Turn。Hermes 的 leftover handoff 说明允许结束时也必须交代未消费输入的归宿。
2. **移交期间谁拥有输入和执行权？** Nika 已有 Session-owned buffer、accepted/injected 区分和单 writer。需要用事件和事务协议连接它们：输入只属于当前请求或未来 Turn，finalization、投递和新 Turn 交接都有 owner，旧回调不能抢回写权限。Hermes 的 guard/late-drain 只能作为测试场景，不是 Nika 的持久性证明。
3. **停止完成证明了什么？** 应分开表示“已发取消”“执行已退出”“结果或副作用未知”“清理仍待处理”。每个未结束 Tool Call 都需要关联结果，资源 owner 要隔离迟到结果。保留 Nika 的未知状态和 Pending steer 规则，不把合成 cancelled 当成彻底停止。

建议覆盖：steer 在模型返回前、返回后但 final commit 前、commit 后、清理 await 中到达；stop 与 steer 并发；Tool A 完成而 Tool B 运行时 stop；工具不响应取消；持久化或清理失败。分别检查输入归属、调用/结果配对、下一请求内容、writer 交接和用户可见结束语义。无需因此引入 Router、额外 task queue 或硬调用预算。

局限：本轮未遍历所有入口、进程间协议、存储迁移和重启恢复。补充抓取曾遇到 GitHub Raw HTTP 429，必要片段改经官方 Contents API 获取；本文只引用成功读取的范围。

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
