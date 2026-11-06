# 同批工具调用：前项失败后是否继续

核验日期：2026-11-03。本文回应 T1/T3 的问题：一次模型响应提出多个 Tool Call，某项普通失败后，尚未执行的调用是否继续。本文最初记录源码事实与待确认建议；**用户随后于同日确认“普通错误继续，有依赖动作分两次模型调用”**，权威执行契约现见 [AgentLoop §5.2](../design/agent-loop.md#tool-batch-failure-policy)。下文保留比较依据和未选方案；确认设计不代表已实现或验证。“普通 Tool 错误不自动结束 Turn”见 [§5.1](../design/agent-loop.md#ordinary-tool-errors)。

后续决定：用户于同日确认第 4 节的超时与未知结果建议，正式契约见 [AgentLoop §5.3](../design/agent-loop.md#tool-timeout-outcomes)。普通错误与未知结果保持不同处理；设计确认不代表实现完成。

范围：静态读取固定版本的源码和测试，未运行外部代码或测试。Hermes 为 `NousResearch/hermes-agent@d7572ceda754b888216c212691e1b2301ebe0359`；Yomi 比较 fork `12bitsD/yomi@7ec60e80b6b3b2f8020ba712f0b723d1d66f8a97` 与 upstream `Crescent617/yomi@2193746a4a2db2c17a62c5e66ba66a2c4e25056a`。Turn/steer 背景分别见 [Hermes](2026-11-03-hermes-turn-control.md) 和 [Yomi](2026-11-03-yomi-turn-control.md)。

## 1. Hermes：串行路径通常继续

串行执行器将工具返回值分类为成功或错误，提交对应 Tool Result；提交成功后仍返回 True。外层循环没有因为 `is_error` 而停止，因此一个普通错误通常不阻止下一项。参数解析失败也在提交错误结果后继续。[结果提交][h-result]、[串行循环][h-loop]

这不等于所有异常都继续：Registry/context/memory 等路径配置了异常到错误结果的转换；未配置转换的执行异常可外溢，离开当前批次。持久化失败阻止后续派发；取消会为未启动调用补出 skipped/cancelled 结果。[异常转换][h-dispatch]、[异常上抛][h-exception]、[串行循环][h-loop]

有一个直接相关的测试：第一个终端命令返回 exit code 1，第二个命令重新经过审批后仍被执行并返回 0。前项失败会使后续预先取得的批次审批失效，后项仍须满足运行时准入条件。这里仅核对测试断言，未运行测试。[失败后继续测试][h-test]

并行路径中，单个 worker 的 Exception 被转为该项错误结果，其余可执行调用已被提交，不因这一普通错误自动取消整批。[并行路径][h-parallel]

边界：另一个测试明确允许串行超时后继续下一项，同时将前项 `effect_disposition` 标为 unknown；预准备终端批次又有不同处理。因此不能将 Hermes 的超时策略直接引入 Nika，也不能把未知副作用当成普通、已知的失败。[超时测试][h-timeout]

## 2. Yomi：先并发派发，再分别收集结果

fork 与 upstream 都在权限检查后，为所有获准调用创建并发任务，再进入结果收集循环。因此一个调用报错时，其他调用可能已经开始或完成；这里没有可靠的“前一个先失败，后一个尚未执行”的顺序。[fork 派发/收集][y-batch]、[upstream 派发/收集][y-up-batch]

`tool.exec()` 的普通 Err 被转成 `ToolOutput::error`；收集循环逐项记录结果，没有根据 `is_error` 停止其他调用。结果保留原 `tool_call_id`，同批完成后回到下一次模型请求。[错误转换][y-executor]、[fork 派发/收集][y-batch]

Run 的取消信号另行处理：abort 所有任务，收集已完成结果，只为尚无结果的调用补 cancelled，并返回取消状态。不能把“某个工具返回包含 cancelled 的错误”与“Run 已被取消”混为一谈。[fork 取消路径][y-cancel]

错误表示也需单独设计：同步 Shell 的超时返回错误结果，但非零退出码写在输出正文中，工具结果本身可仍为 `is_error=false`。因此仅靠通用错误标志不能完整判断“测试未通过”之类的操作结果。[Shell 返回值][y-shell]

检查到的测试覆盖 missing-tool 的调用 ID，以及成功结果与取消结果在同一批中的配对；未找到直接断言“A 普通失败、B 仍执行”的专门用例。本节普通失败继续的结论来自源码控制流。upstream 的重复调用检测发生在整批结束后，不是单项失败即停。[配对测试][y-id-test]、[取消测试][y-cancel-test]、[重复检测][y-repeat]

## 3. Nika 的取舍：执行顺序不等于成功依赖

Nika 已选择首版串行；这只保证 A 先执行、B 后执行，**不自动表达“只有 A 成功才能执行 B”**。需要分别看两种场景：

- 同时读取两个独立文件：A 不存在，B 仍然有用。继续执行 B，可以将两个结果一起交给模型。
- 先修改文件，再验证这次修改：B 的意义依赖 A 确实成功。模型应先收到 A 的结果，再决定 B；测试通过才能发布也属于这种成功依赖。

因此，所谓“同批无依赖”必须包含两件事：**B 的参数不依赖 A 的结果，B 是否应执行也不依赖 A 的成功。** 仅仅提前知道 B 的参数还不够。分成两次模型调用仍在同一个 Turn/Run 内，不要求用户再发消息，也不建立新 Session。

讨论时比较了两种策略，区别如下；用户已选择第一种：

| 候选 | 行为 | 代价与限制 |
|---|---|---|
| 普通错误后继续 | 保存 A 的真实错误，逐项执行后续获准调用，汇总结果交模型 | 独立调用不额外等待模型；若模型把依赖动作误放同批，运行时不会自动识别和阻止 |
| 普通错误后暂停整批 | 保存 A 的错误，给尚未执行项记录未执行结果，将情况交模型重新决定 | 可阻止失败之后尚未开始的动作；独立调用也可能多一次模型往返，且仍不能防住工具错误标志未反映的业务失败 |

**研究建议，已于同日获用户确认：普通错误默认继续；有结果或成功依赖的动作分两次模型调用。** 这比此前“普通错误就暂停整批”的建议更适合独立检索等常见批次，并与现有 [System Prompt](https://github.com/12bitsD/nika/blob/main/docs/design/system-prompt.md) 的“不提前假设上一步结果”一致。它是模型调用约定，不是已被 Runtime 证明的独立性；不能据此放松工具本身的授权和必要前置检查。暂不增加自动依赖推断、依赖图或批次事务。

两种候选均须保留这些边界：结果提交后才能开始下一项；后续调用仍各自接受授权和资源检查；继续 B 不代表重试 A；取消、执行权失效、必需持久化失败与未知副作用单独处理。本次不决定坏调用、超时和未知结果的全部分支，也不改变既有恢复规则。

采纳后的待完成验证至少覆盖：A 读取失败而 B 独立读取成功；A 修改失败后模型改方案，再提出验证动作；普通错误与取消/提交失败分别发生时的派发差异；所有已提出调用的结果身份完整。还须用模型场景验证是否遵守依赖动作跨请求的约定，不能仅用执行器单元测试证明模型会正确分批。

<a id="tool-timeout-outcomes"></a>

## 4. 超时与未知结果（已确认，待实现）

### 源码事实

**Hermes：**普通串行工具到期后发出线程取消信号、调用 `future.cancel()`，然后以 `shutdown(wait=False)` 放弃等待，并不确认正在运行的 worker 已退出。超时结果记录 `effect_disposition=unknown`，普通路径仍执行后续调用。对应测试的假工具在整批返回后才被释放，却已观察到第二项执行；这是测试断言，未在本轮运行。[超时处置][h-timeout-runtime]、[发布结果][h-result]、[测试][h-timeout]

内部记录 unknown 也不等于模型看见这一事实：所检查的 Chat Completions transport 去掉 `effect_disposition`，实时超时正文只有 timed out 错误；历史恢复路径另有明确的“effect UNKNOWN，重试前先检查”提示，不能当作实时超时路径的统一约束。预准备终端批次则会在超时时 close 批次并触发 interrupt，阻止后续命令；这是一条特定路径，不能推广到全部工具。[模型字段过滤][h-timeout-wire]、[过滤调用][h-timeout-wire-call]、[恢复提示][h-replay-unknown]、[终端批次特例][h-terminal-timeout]

**Yomi：**两份固定版本的同步 Shell 底层尝试终止进程组并等待主进程退出；`kill_tree` 忽略 kill/wait 错误，没有返回结构化终止确认，因此不能证明所有后裔或远端操作均已停止。底层继续收集部分输出、保留成功写出的日志引用，但 Shell 的 timeout 分支提前返回 `Command timed out`，没有将这些证据带入 Tool Result。该错误不取消 Run token，其他并发调用照常收集，之后回到模型。[超时路径][y-timeout-runtime]、[终止实现][y-kill-tree]、[部分输出][y-timeout-partial]、[Shell 返回][y-timeout-return]、[批次收集][y-up-batch]

Yomi 有进程组终止和输出排空相关测试，但不能据此声称 Shell 模型结果已保留部分输出；本轮未运行测试。上述事实说明可借鉴进程清理与结果记录的职责划分，但两者都不能直接证明 Nika 所需的未知结果处理已经充分。[进程组测试][y-process-test]、[输出排空测试][y-partial-test]

### Nika 采纳的规则

超时只说明期限内没有取得预期的完整结果。**执行是否仍在运行，与已经发生的业务效果是否已知，必须分开。** 杀死写文件进程不会撤销已写入内容；发送飞书评论的请求超时，也可能是评论已创建、响应没有返回。工具名或一段看起来像读取的 shell 命令，不能替代工具契约及实际执行事实。

此前已确认未知如实记录、不盲目重放、结果提交成功才继续，以及取消必须收尾。用户随后确认以下建议，现已写入 AgentLoop §5.3；此处保留研究依据与采纳记录，后续执行契约以 design 文档为准：

1. 超时后由 ToolRuntime 请求停止并收集实际结果、部分输出及清理结果，清理有明确期限。如果此时取得可靠的真实结果，应保留它，不能被合成 timeout/unknown 覆盖。
2. 若确认执行已停，且工具契约与实际事实能够排除未知副作用，可以按普通错误处理，继续同批后项。例如确实只做查询的操作超时并已退出。不能仅凭“进程已退出”就把可能完成或部分完成的写入归为普通失败。
3. 如果效果未知，结束当前批次：保留已完成结果，将尚未启动项记录为未执行及原因，完成调用/结果配对并提交。执行已退出或已可靠隔离、且存在经 Runtime 准入的核查方式时，将这些事实交回模型，在同一 Turn/Run 中先检查实际状态。后续动作是模型提出的新调用，旧批次不自动恢复，也不自动重试未知动作。
4. 如果无法确认旧执行退出或隔离，不放行与它冲突的工具；无法安全继续时收尾并报告受阻。相关资源不能因为本 Run 收尾，就被下一 Turn 或其他 Session 当作空闲资源使用。资源的具体隔离、不可用标记或责任接管协议仍由 T3 定义，不在这里增加后台恢复调度。

模型看到的结果应说明未知范围和可用证据，而不只是一句 timeout。一次查询“没找到”也可能受远端延迟影响，不能自动证明可以重复写入；是否可重试应依据实际状态或工具的幂等保证，可核验的操作身份用于关联证据，不单独证明可以重试。上述同 Turn 核查仅针对实时超时，不允许用户 `/stop` 后借此继续，也不改变进程重启后不自动恢复旧执行的决定。

代价是：一项未知结果会使同批其余独立调用也暂不执行，需要模型重新决定。验证应覆盖查询超时、写入已完成但响应丢失、进程已停但部分写入、无法终止的执行、清理期间真实结果返回，以及超时结果中的部分输出。实际执行、模型可见结果、调用身份和资源状态应分别核对；实现与验证均未完成。

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
