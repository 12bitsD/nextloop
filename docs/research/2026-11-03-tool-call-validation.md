# 工具调用校验：整批拒绝与错误反馈

核验日期：2026-11-03。范围为 T1/T2/T3 交接处：模型提出的 Tool Call 何时可以交给 ToolRuntime。用户随后选择**任一格式/Schema 校验错误均整批不执行、直接报错**，正式契约见 [AgentLoop §5.4](../design/agent-loop.md#tool-call-validation)。此前“只拒绝坏项”的建议未采纳；源码事实保持原样。已确认的 [普通错误同批继续](../design/agent-loop.md#tool-batch-failure-policy) 适用于通过校验后的运行错误，[超时/unknown 处理](../design/agent-loop.md#tool-timeout-outcomes) 也保持独立。设计已确认，实现和验证未完成。

版本：Hermes `NousResearch/hermes-agent@d7572ceda754b888216c212691e1b2301ebe0359`；Yomi fork `12bitsD/yomi@7ec60e80b6b3b2f8020ba712f0b723d1d66f8a97`，并核对 upstream `Crescent617/yomi@2193746a4a2db2c17a62c5e66ba66a2c4e25056a` 的对应路径。只读源码和测试文本，未执行外部代码或测试；以下实现事实均限于所引用路径。

## 1. Hermes：主循环校验与下层执行器不能混为一谈

- **工具不存在：**先尝试名称修复；仍未知的项写入关联错误、移出执行列表。混合批次中的有效项在其余校验通过后继续。名称自动修复是 Hermes 的选择，不是 Nika 已接受的契约。[名称判断][h-name]、[错误与过滤][h-filter]
- **参数语法无效：**原生主循环先检查整批 JSON。疑似截断形态进入 partial 返回；其他语法错误先有限次重新请求模型，随后为整批记录错误/跳过结果，不执行有效的兄弟项。该检查在 dispatch 前。不能据此推断 Nika 应复制重试次数或截断启发式。[主循环校验][h-json]、[校验入口][h-entry]
- **下层解析错误：**executor 本身可为单项解析错误写结果并继续；相关测试直接调用 executor，绕过上层整批校验，因此不证明完整原生 Loop 也逐项继续。合法 JSON 非对象的拒绝同样不代表完整 JSON Schema 校验。[executor 解析][h-parse]、[循环][h-exec]、[测试入口][h-test]
- **调用身份：**主循环会检查重复 ID，包含历史 ID，并为重复的新 ID 加后缀；空 ID 留给后续 fallback。本轮未完整核对所有 adapter 的空 ID 配对链路，不声称其统一保证。正常 dispatch 前先保存 assistant calls，保存失败阻止执行。[身份入口][h-id-entry]、[重复处理][h-id]、[执行前保存][h-persist]

## 2. Yomi：不同 Provider 解析路径产生不同执行结果

工具不存在时，executor 生成 `Unknown tool` 错误结果，其他获准调用继续。所读公共执行路径直接将 `Value` 交给 `tool.exec`，没有统一按 Tool schema 校验的步骤；具体工具各自检查参数。例如 Shell 缺少必需的字符串 command 会报错，部分可选字段类型不对则可能使用默认值。[未知工具][y-unknown]、[公共执行][y-executor]、[Shell 参数][y-shell]

参数不是合法 JSON 时，OpenAI Chat 和 Anthropic 路径可降为 `Value::String` 后继续产出 ToolCall；OpenAI Responses 路径返回 Parse error，Agent 不派发该次响应收集到的调用。这说明 Provider adapter 的宽松解析会改变执行行为，不能概括成统一的“坏参数拒绝”。[Chat][y-chat]、[Anthropic][y-anthropic]、[Responses][y-responses-parse]、[Agent 收集][y-collect]

流式 ToolCall/Delta 先收集或展示，不直接执行。但 Responses 有一个明确的截断例外：收到 `response.incomplete` 时，保留已经 `output_item.done` 的完整调用，丢弃尾部 partial，归一为 ToolCalls，允许完整子集执行；`response.completed` 却留下未完成调用则报协议错误。两种行为都有测试文本，未在本轮运行。[截断实现][y-incomplete]、[upstream 对应实现][y-up-incomplete]、[测试][y-incomplete-test]

所读 collector 直接追加调用；executor 的 message ID map 以 call ID 为 key，却仍遍历全部调用派发，未发现统一的重复 ID 拒绝步骤。静态上可能出现多个执行共用结果身份；本轮未复现，也未找到直接覆盖此情况的拒绝测试。缺失 ID 的处理也分 Provider：Chat/Responses 可丢弃该项，而非统一形成错误结果。不能把这些局部处理视为 Nika 所需的完整配对保证。[collector][y-ids-collect]、[ID map 与派发][y-ids-exec]、[Chat 完成][y-chat]、[Responses 解析][y-responses-parse]

## 3. Nika 已确认：执行前统一校验，任一错误拒绝整批

用户选择的简化规则是：模型返回的调用若不符合工具定义或协议要求，直接报错，这批调用全部未执行。完整响应的记录、调用是否可执行、能否构造下一次合法模型请求仍是三个不同问题。

1. **任何副作用前，完成整批检查。** 根据 Provider 协议组装流式片段，检查响应完整性、调用身份、工具名称、参数编码及 Schema。全部通过才能开始执行；不会因为前项看起来正确就先执行它，也不执行截断响应中的完整子集。
2. **任一项不合法，整批不执行并报错。** 工具不存在、缺必填字段、参数类型错误、坏 JSON 与整批身份/完整性问题使用相同的拒绝范围。例：`read` 缺 path，则同批另一个合法读取也不执行。校验不调用工具试运行，不自动修名、修 JSON 或猜填参数。
3. **保留事实并明确未执行。** 身份有效时，可以关联具体错误和其余项因整批拒绝而未执行的反馈；身份无法关联时保存批次协议诊断，不伪造 ID 或正常结果。错误如何成为合法 Provider 消息交 T2 定义，不能将无效响应原样当作正常历史继续发送。

单项坏 JSON 同样拒绝整批，但即使有 ID，也不能假定每个 Provider 允许在下一请求中回传同样的非法 arguments。具体反馈样例与投影留待 T2 明确。工具契约明确声明的默认值、Runtime 注入字段和 Provider 规定的解析/身份映射仍正常使用；这不等于替模型猜补缺失内容。

身份检查限定于当前逻辑响应的关联完整性；同一调用的多个流式 delta 不是重复调用，不同 `model_call_id` 恰好复用 Provider ID 也不自动证明重复执行。内部关联需要保留逻辑请求身份和 Provider 身份，确切字段及 Provider 要求的唯一范围由 T2/T4 定义。缺少一个并非该 Provider 必需的字段，不应仅据其他 Provider 的格式判作异常。

## 4. 选择理由、剩余工作与验证

**此前候选，未采纳：**能可靠关联的名称/参数错误只拒绝该项、继续合法独立项；只有身份歧义或截断才整批拒绝。这可以减少独立调用的等待，但需要两种拒绝范围。用户选择统一整批校验报错，使执行前行为更简单；代价是坏项也会阻止同批合法项。此变化来自用户取舍，不是新的源码事实推翻旧研究。

校验拒绝不等于执行后失败：通过校验的读取发现路径不存在，仍沿用普通错误续批；缺少 path 参数则整批未执行。这不是工具副作用事务，不引入执行后的回滚。

本次只确认拒绝和报错，不默认自动重试模型，也不默认终止 Turn。合法反馈是后续修正请求的必要条件，不自动触发修正；修正触发、轮数和 Provider 样例仍交 T1/T2。若允许模型修正，则属于同一 Turn/Run 内的新逻辑请求；已有输出不能被当作“无输出请求”原样重试，也不能改写已提交过的 Provider 可见前缀。可识别、被拒的调用沿用软调用预算；不能从无法识别的响应中虚构调用或执行次数，模型 usage 仍如实记录。

职责：ModelProvider/model invocation 提供完整性及协议事实；Tool Catalog/Runtime 提供名称、schema 与运行前检查；Tool batch flow 关联并保存错误；driver 决定续调或收尾；ContextBuilder 只发送可被 Provider 接受的投影。保存一份模型事实不等于允许执行，更不等于所有原始字段都能直接成为合法模型历史。

待验证：单项缺参数＋独立合法项、unknown tool＋合法项、坏 JSON、同批重复 ID、身份缺失/为空及前项完整而后项截断，均须确认本批执行次数为零。另测正常流式多段同 ID、跨请求复用 ID、校验通过后的普通失败续批，并检查完整事实记录、合法反馈与调用/结果配对。实现、测试和 Provider 实测均未完成。

[h-name]: https://github.com/NousResearch/hermes-agent/blob/d7572ceda754b888216c212691e1b2301ebe0359/agent/turn_tool_validation.py#L101-L127
[h-filter]: https://github.com/NousResearch/hermes-agent/blob/d7572ceda754b888216c212691e1b2301ebe0359/agent/turn_tool_round.py#L104-L128
[h-json]: https://github.com/NousResearch/hermes-agent/blob/d7572ceda754b888216c212691e1b2301ebe0359/agent/turn_tool_validation.py#L157-L232
[h-entry]: https://github.com/NousResearch/hermes-agent/blob/d7572ceda754b888216c212691e1b2301ebe0359/agent/turn_tool_round.py#L80-L88
[h-parse]: https://github.com/NousResearch/hermes-agent/blob/d7572ceda754b888216c212691e1b2301ebe0359/agent/tool_executor.py#L175-L186
[h-exec]: https://github.com/NousResearch/hermes-agent/blob/d7572ceda754b888216c212691e1b2301ebe0359/agent/tool_executor.py#L1892-L1897
[h-test]: https://github.com/NousResearch/hermes-agent/blob/d7572ceda754b888216c212691e1b2301ebe0359/tests/agent/test_malformed_tool_arguments.py#L49-L97
[h-id-entry]: https://github.com/NousResearch/hermes-agent/blob/d7572ceda754b888216c212691e1b2301ebe0359/agent/turn_tool_validation.py#L89-L99
[h-id]: https://github.com/NousResearch/hermes-agent/blob/d7572ceda754b888216c212691e1b2301ebe0359/agent/message_sanitization.py#L550-L592
[h-persist]: https://github.com/NousResearch/hermes-agent/blob/d7572ceda754b888216c212691e1b2301ebe0359/agent/turn_tool_round.py#L130-L171
[y-unknown]: https://github.com/12bitsD/yomi/blob/7ec60e80b6b3b2f8020ba712f0b723d1d66f8a97/crates/kernel/src/agent/agent/tool_exec.rs#L476-L510
[y-executor]: https://github.com/12bitsD/yomi/blob/7ec60e80b6b3b2f8020ba712f0b723d1d66f8a97/crates/kernel/src/tools/executor.rs#L178-L188
[y-shell]: https://github.com/12bitsD/yomi/blob/7ec60e80b6b3b2f8020ba712f0b723d1d66f8a97/crates/kernel/src/tools/shell.rs#L133-L140
[y-chat]: https://github.com/12bitsD/yomi/blob/7ec60e80b6b3b2f8020ba712f0b723d1d66f8a97/crates/kernel/src/provider/openai.rs#L588-L602
[y-anthropic]: https://github.com/12bitsD/yomi/blob/7ec60e80b6b3b2f8020ba712f0b723d1d66f8a97/crates/kernel/src/provider/anthropic.rs#L514-L523
[y-responses-parse]: https://github.com/12bitsD/yomi/blob/7ec60e80b6b3b2f8020ba712f0b723d1d66f8a97/crates/kernel/src/provider/openai_response.rs#L606-L638
[y-collect]: https://github.com/12bitsD/yomi/blob/7ec60e80b6b3b2f8020ba712f0b723d1d66f8a97/crates/kernel/src/agent/agent.rs#L1187-L1264
[y-incomplete]: https://github.com/12bitsD/yomi/blob/7ec60e80b6b3b2f8020ba712f0b723d1d66f8a97/crates/kernel/src/provider/openai_response.rs#L507-L555
[y-up-incomplete]: https://github.com/Crescent617/yomi/blob/2193746a4a2db2c17a62c5e66ba66a2c4e25056a/crates/kernel/src/provider/openai_response.rs#L508-L556
[y-incomplete-test]: https://github.com/12bitsD/yomi/blob/7ec60e80b6b3b2f8020ba712f0b723d1d66f8a97/crates/kernel/src/provider/openai_response_test.rs#L696-L743
[y-ids-collect]: https://github.com/12bitsD/yomi/blob/7ec60e80b6b3b2f8020ba712f0b723d1d66f8a97/crates/kernel/src/agent/stream_collector.rs#L111-L117
[y-ids-exec]: https://github.com/12bitsD/yomi/blob/7ec60e80b6b3b2f8020ba712f0b723d1d66f8a97/crates/kernel/src/agent/agent/tool_exec.rs#L350-L458
