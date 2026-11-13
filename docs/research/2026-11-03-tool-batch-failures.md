# 同批工具调用：前项失败后是否继续

核验日期：2026-11-03。问题：一次模型响应提出多个 Tool Call，某项普通失败后，尚未执行的调用是否继续。

**同日已确认：普通错误继续；有结果或成功依赖的动作分两次模型调用。** 普通错误不自动结束 Turn。超时和未知结果使用另一套规则，不能当成普通错误。设计确认不代表已经实现或验证。

当前执行契约见 [AgentLoop](../design/agent-loop.md)。本文保留源码依据和未选方案。Turn/steer 背景见 [Hermes](2026-11-03-hermes-turn-control.md) 和 [Yomi](2026-11-03-yomi-turn-control.md)。

范围：静态读取固定版本源码和测试，未运行外部代码或测试。版本：

- Hermes：`NousResearch/hermes-agent@d7572ceda754b888216c212691e1b2301ebe0359`
- Yomi fork：`12bitsD/yomi@7ec60e80b6b3b2f8020ba712f0b723d1d66f8a97`
- Yomi upstream：`Crescent617/yomi@2193746a4a2db2c17a62c5e66ba66a2c4e25056a`

## 1. Hermes：串行路径通常继续

串行执行器将返回值分类为成功或错误，提交对应 Tool Result 后仍返回 True。外层循环不会因为 `is_error` 停止，因此普通错误通常不阻止下一项。参数解析失败也在提交错误结果后继续。[结果提交][h-result]、[串行循环][h-loop]

但不是所有异常都继续：

- Registry/context/memory 等路径配置了异常到错误结果的转换；未配置转换的执行异常可以离开当前批次。
- 持久化失败会阻止后续派发。
- 取消会为未启动调用补 skipped/cancelled 结果。[异常转换][h-dispatch]、[异常上抛][h-exception]、[串行循环][h-loop]

相关测试中，第一个终端命令返回 exit code 1，第二个命令重新经过审批后仍执行并返回 0。前项失败会使后续预先取得的批次审批失效，后项仍须满足运行时准入。本轮只核对测试断言，未运行测试。[失败后继续测试][h-test]

并行路径中，单个 worker 的 Exception 会转成该项错误结果，其余已提交调用不会因此自动取消。[并行路径][h-parallel]

另一个测试允许串行超时后继续下一项，同时将前项 `effect_disposition` 标为 unknown；预准备终端批次有不同处理。因此，Hermes 的超时策略不能直接引入 Nika，未知副作用也不能当成普通失败。[超时测试][h-timeout]

## 2. Yomi：先并发派发，再分别收集结果

fork 和 upstream 都在权限检查后为所有获准调用创建并发任务，再进入结果收集循环。一个调用报错时，其他调用可能已开始或完成，不存在可靠的“前一个失败后一个尚未执行”的顺序。[fork 派发/收集][y-batch]、[upstream 派发/收集][y-up-batch]

`tool.exec()` 的普通 Err 会转成 `ToolOutput::error`。收集循环逐项记录，没有根据 `is_error` 停止其他调用。结果保留原 `tool_call_id`，同批完成后回到下一次模型请求。[错误转换][y-executor]、[fork 派发/收集][y-batch]

Run 取消是另一条路径：abort 所有任务，收集已完成结果，只为尚无结果的调用补 cancelled，并返回取消状态。工具结果包含 cancelled 不等于整个 Run 已取消。[fork 取消路径][y-cancel]

错误标志也不能表达所有业务结果：同步 Shell 超时返回错误结果，但非零退出码写在正文中，Tool Result 本身仍可为 `is_error=false`。因此不能只看通用错误标志判断测试是否通过。[Shell 返回值][y-shell]

检查到的测试覆盖 missing-tool 的调用 ID，以及成功和取消结果在同批中的配对；未找到直接断言“A 普通失败、B 仍执行”的专门用例。本节普通失败继续的结论来自源码控制流。upstream 的重复调用检测发生在整批结束后，不是单项失败即停。[配对测试][y-id-test]、[取消测试][y-cancel-test]、[重复检测][y-repeat]

## 3. Nika 的取舍：执行顺序不等于成功依赖

首版串行只保证 A 先执行、B 后执行，**不自动表达“只有 A 成功才能执行 B”**。

- 同时读取两个独立文件：A 不存在，B 仍然有用，应继续 B 并一起交给模型。
- 先修改文件，再验证修改：B 的意义依赖 A 确实成功。模型应先看到 A 的结果，再决定 B。测试通过才能发布也是成功依赖。

因此，“同批无依赖”包含两点：**B 的参数不依赖 A 的结果，B 是否应执行也不依赖 A 的成功。**仅提前知道参数还不够。分成两次模型调用仍在同一个 Turn/Run 内，不需要用户再发消息，也不建立新 Session。

| 候选 | 行为 | 代价与限制 |
|---|---|---|
| 普通错误后继续（已选） | 保存 A 的真实错误，逐项执行后续获准调用，汇总给模型 | 独立调用不额外等待模型；如果模型误把依赖动作放入同批，运行时不会自动识别 |
| 普通错误后暂停整批 | 保存 A 的错误，将未执行项标记后交给模型重新决定 | 可阻止失败后的未开始动作，但独立调用也会增加模型往返；仍不能识别错误标志未表达的业务失败 |

**已确认：普通错误默认继续；有结果或成功依赖的动作分两次模型调用。**这适合独立检索等常见批次，也符合 [System Prompt](https://github.com/12bitsD/nika/blob/main/docs/design/system-prompt.md) 的“不提前假设上一步结果”。它是模型调用约定，不是 Runtime 已证明的独立性，不能放松授权和必要前置检查。暂不增加自动依赖推断、依赖图或批次事务。

两种候选都必须遵守：结果提交后才能开始下一项；后续调用仍各自接受授权和资源检查；继续 B 不代表重试 A；取消、执行权失效、持久化失败和未知副作用单独处理。

后续验证至少覆盖：A 读取失败而 B 独立读取成功；A 修改失败后模型改方案再验证；普通错误与取消/提交失败的派发差异；所有调用的结果身份完整。还需要模型场景验证依赖动作是否跨请求，不能只用执行器单元测试证明模型会正确分批。

<a id="tool-timeout-outcomes"></a>

## 4. 超时与未知结果

### Hermes 的源码事实

普通串行工具到期后发出线程取消信号、调用 `future.cancel()`，再以 `shutdown(wait=False)` 放弃等待，并不确认 worker 已退出。超时结果记录 `effect_disposition=unknown`，普通路径仍执行后续调用。[超时处置][h-timeout-runtime]、[发布结果][h-result]

对应测试的假工具在整批返回后才被释放，却已观察到第二项执行。这是测试断言，本轮未运行。[测试][h-timeout]

内部记录 unknown 不等于模型能看到它：所检查的 Chat Completions transport 去掉 `effect_disposition`，实时超时正文只有 timed out 错误。历史恢复路径另有“effect UNKNOWN，重试前先检查”的提示，不能当作实时路径的统一约束。[模型字段过滤][h-timeout-wire]、[过滤调用][h-timeout-wire-call]、[恢复提示][h-replay-unknown]

预准备终端批次会在超时时 close 批次并触发 interrupt，阻止后续命令。这是特定路径，不能推广到全部工具。[终端批次特例][h-terminal-timeout]

### Yomi 的源码事实

两份固定版本的同步 Shell 尝试终止进程组并等待主进程退出。`kill_tree` 忽略 kill/wait 错误，没有结构化终止确认，因此不能证明所有后裔或远端操作已停止。[超时路径][y-timeout-runtime]、[终止实现][y-kill-tree]

底层继续收集部分输出并保留成功写出的日志引用，但 Shell 的 timeout 分支提前返回 `Command timed out`，没有把这些证据带入 Tool Result。该错误不取消 Run token，其他并发调用照常收集，之后回到模型。[部分输出][y-timeout-partial]、[Shell 返回][y-timeout-return]、[批次收集][y-up-batch]

Yomi 有进程组终止和输出排空测试，但不能据此声称 Shell 的模型结果已保留部分输出。本轮未运行测试。[进程组测试][y-process-test]、[输出排空测试][y-partial-test]

### Nika 采纳的规则

超时只说明期限内没有取得完整结果。**执行是否仍在运行，与业务效果是否已知，必须分开。**杀死写文件进程不会撤销已写入内容；发送评论的请求超时，也可能评论已创建、响应未返回。工具名或一段看似读取的 shell 命令不能替代实际执行事实。

已确认的规则：

1. 超时后由 ToolRuntime 请求停止，收集实际结果、部分输出和清理结果，清理有明确期限。如果取得可靠真实结果，应保留它，不能被合成 timeout/unknown 覆盖。
2. 如果确认执行已停，且工具契约和实际事实能排除未知副作用，可以按普通错误继续。例如只做查询的操作超时并已退出。不能仅凭进程退出，就把可能完成的写入归为普通失败。
3. 如果效果未知，结束当前批次：保留已完成结果，将未启动项记录为未执行及原因，完成调用/结果配对并提交。执行已退出或可靠隔离，且 Runtime 允许核查时，将这些事实交回模型，在同一 Turn/Run 中先检查实际状态。后续动作是模型提出的新调用，旧批次不自动恢复或重试。
4. 如果无法确认旧执行退出或隔离，不放行冲突工具；无法安全继续时收尾为受阻。相关资源不能因为本 Run 收尾，就被下一 Turn 或其他 Session 当作空闲。具体隔离和接管协议由 T3 定义，不在这里增加后台恢复调度。

模型看到的结果应说明未知范围和可用证据，而不只是 timeout。“查询没找到”也可能受远端延迟影响，不能自动证明可以重复写入。是否重试取决于实际状态或幂等保证；操作身份用于关联证据，本身不证明可以重试。同 Turn 核查只针对实时超时，`/stop` 后不能借此继续，进程重启后也不自动恢复旧执行。

代价是：一项未知结果会让同批其他独立调用也暂不执行，需要模型重新决定。验证应覆盖查询超时、写入完成但响应丢失、进程已停但部分写入、无法终止的执行、清理期间真实结果返回，以及超时结果中的部分输出。实际执行、模型可见结果、调用身份和资源状态要分别核对。设计确认不代表实现与验证已经完成。

[h-result]: https://github.com/NousResearch/hermes-agent/blob/d7572ceda754b888216c212691e1b2301ebe0359/agent/tool_executor.py#L1810-L1854
[h-loop]: https://github.com/NousResearch/hermes-agent/blob/d7572ceda754b888216c212691e1b2301ebe0359/agent/tool_executor.py#L1876-L1924
[h-dispatch]: https://github.com/NousResearch/hermes-agent/blob/d7572ceda754b888216c212691e1b2301ebe0359/agent/tool_executor.py#L1658-L1739
[h-exception]: https://github.com/NousResearch/hermes-agent/blob/d7572ceda754b888216c212691e1b2301ebe0359/agent/tool_executor.py#L1756-L1807
[h-test]: https://github.com/NousResearch/hermes-agent/blob/d7572ceda754b888216c212691e1b2301ebe0359/tests/agent/test_terminal_approval_batch.py#L234-L315
[h-parallel]: https://github.com/NousResearch/hermes-agent/blob/d7572ceda754b888216c212691e1b2301ebe0359/agent/tool_executor.py#L1337-L1433
[h-timeout]: https://github.com/NousResearch/hermes-agent/blob/d7572ceda754b888216c212691e1b2301ebe0359/tests/agent/test_sequential_tool_timeout.py#L120-L166
[y-batch]: https://github.com/12bitsD/yomi/blob/7ec60e80b6b3b2f8020ba712f0b723d1d66f8a97/crates/kernel/src/agent/agent/tool_exec.rs#L282-L429
[y-up-batch]: https://github.com/Crescent617/yomi/blob/2193746a4a2db2c17a62c5e66ba66a2c4e25056a/crates/kernel/src/agent/agent/tool_exec.rs#L324-L469
[y-executor]: https://github.com/12bitsD/yomi/blob/7ec60e80b6b3b2f8020ba712f0b723d1d66f8a97/crates/kernel/src/tools/executor.rs#L180-L188
[y-cancel]: https://github.com/12bitsD/yomi/blob/7ec60e80b6b3b2f8020ba712f0b723d1d66f8a97/crates/kernel/src/agent/agent/tool_exec.rs#L301-L347
[y-shell]: https://github.com/12bitsD/yomi/blob/7ec60e80b6b3b2f8020ba712f0b723d1d66f8a97/crates/kernel/src/tools/shell.rs#L295-L353
[y-id-test]: https://github.com/12bitsD/yomi/blob/7ec60e80b6b3b2f8020ba712f0b723d1d66f8a97/crates/kernel/src/agent/agent/tool_exec_test.rs#L7-L48
[y-cancel-test]: https://github.com/12bitsD/yomi/blob/7ec60e80b6b3b2f8020ba712f0b723d1d66f8a97/crates/kernel/src/agent/agent/tool_exec_test.rs#L145-L244
[y-repeat]: https://github.com/Crescent617/yomi/blob/2193746a4a2db2c17a62c5e66ba66a2c4e25056a/crates/kernel/src/agent/agent/tool_exec.rs#L71-L112
[h-timeout-runtime]: https://github.com/NousResearch/hermes-agent/blob/d7572ceda754b888216c212691e1b2301ebe0359/agent/tool_executor.py#L866-L994
[h-timeout-wire]: https://github.com/NousResearch/hermes-agent/blob/d7572ceda754b888216c212691e1b2301ebe0359/agent/transports/chat_completions.py#L37-L45
[h-timeout-wire-call]: https://github.com/NousResearch/hermes-agent/blob/d7572ceda754b888216c212691e1b2301ebe0359/agent/transports/chat_completions.py#L475-L492
[h-replay-unknown]: https://github.com/NousResearch/hermes-agent/blob/d7572ceda754b888216c212691e1b2301ebe0359/agent/replay_cleanup.py#L120-L142
[h-terminal-timeout]: https://github.com/NousResearch/hermes-agent/blob/d7572ceda754b888216c212691e1b2301ebe0359/agent/tool_executor.py#L977-L986
[y-timeout-runtime]: https://github.com/Crescent617/yomi/blob/2193746a4a2db2c17a62c5e66ba66a2c4e25056a/crates/kernel/src/utils/spawn.rs#L159-L200
[y-kill-tree]: https://github.com/Crescent617/yomi/blob/2193746a4a2db2c17a62c5e66ba66a2c4e25056a/crates/kernel/src/utils/process.rs#L118-L166
[y-timeout-partial]: https://github.com/Crescent617/yomi/blob/2193746a4a2db2c17a62c5e66ba66a2c4e25056a/crates/kernel/src/utils/spawn.rs#L202-L244
[y-timeout-return]: https://github.com/Crescent617/yomi/blob/2193746a4a2db2c17a62c5e66ba66a2c4e25056a/crates/kernel/src/tools/shell.rs#L282-L307
[y-process-test]: https://github.com/Crescent617/yomi/blob/2193746a4a2db2c17a62c5e66ba66a2c4e25056a/crates/kernel/src/utils/process_test.rs#L21-L50
[y-partial-test]: https://github.com/Crescent617/yomi/blob/2193746a4a2db2c17a62c5e66ba66a2c4e25056a/crates/kernel/src/utils/spawn_test.rs#L173-L192
