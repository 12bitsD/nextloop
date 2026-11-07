from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

from nextloop import (
    AgentLoop,
    AnthropicProvider,
    Completed,
    EventType,
    FinishReason,
    InMemoryEventStore,
    LoopConfig,
    Message,
    ModelResponse,
    OpenAIProvider,
    ToolCall,
    ToolExecutionMode,
    ToolRegistry,
    ToolSpec,
    TurnContext,
)

MODEL = "kimi-for-coding"
OPENAI_URL = "https://api.kimi.com/coding/v1"
ANTHROPIC_URL = "https://api.kimi.com/coding/"


class RecordingProvider:
    def __init__(self, delegate):
        self.delegate = delegate
        self.requests = []

    async def complete(self, request, *, attempt, cancel):
        self.requests.append(request)
        return await self.delegate.complete(request, attempt=attempt, cancel=cancel)


class ScriptedProvider:
    def __init__(self, responses, delegate=None):
        self.responses = list(responses)
        self.delegate = delegate

    async def complete(self, request, *, attempt, cancel):
        if self.responses:
            return self.responses.pop(0)
        if self.delegate is None:
            raise AssertionError("ScriptedProvider responses exhausted")
        return await self.delegate.complete(request, attempt=attempt, cancel=cancel)


class Delivery:
    def __init__(self, failures=0):
        self.failures = failures
        self.messages = []

    async def deliver(self, message):
        self.messages.append(message)
        if self.failures:
            self.failures -= 1
            raise RuntimeError("injected delivery failure")


def openai_provider():
    return RecordingProvider(
        OpenAIProvider(
            model_id=MODEL,
            base_url=OPENAI_URL,
            api_key=os.environ["KIMI_API_KEY"],
            default_parameters={"max_tokens": 512},
            timeout_seconds=90,
        )
    )


def config(**kwargs):
    kwargs.setdefault(
        "system_prompt", "你是 nextloop 的 smoke 测试执行器。严格按用户要求调用工具并简短回复。"
    )
    kwargs.setdefault("retry_backoff_seconds", 0)
    return LoopConfig(**kwargs)


def context(store, provider, tools, delivery, text, **kwargs):
    return TurnContext(
        session_id=kwargs.pop("session_id", "live"),
        initial_input=text,
        config=kwargs.pop("config", config()),
        store=store,
        provider=provider,
        tools=tools,
        delivery=delivery,
        **kwargs,
    )


def read_spec(mode=ToolExecutionMode.PARALLEL):
    return ToolSpec(
        "read",
        "读取一个测试事实",
        {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        mode=mode,
    )


def write_spec(**kwargs):
    return ToolSpec(
        "write", "执行一次测试写入", {"type": "object"}, resources=("workspace",), **kwargs
    )


def registry(read_handler=None, write_handler=None, write_timeout=None):
    tools = ToolRegistry()
    tools.register(read_spec(), read_handler or (lambda arguments, ctx: "NEXTLOOP_TOOL_FACT"))
    tools.register(
        write_spec(timeout_seconds=write_timeout),
        write_handler or (lambda arguments, ctx: Completed("written")),
    )
    return tools


@dataclass
class Scenario:
    name: str
    detail: str


def passed(name, detail=""):
    print(f"PASS {name}: {detail}", flush=True)
    return Scenario(name, detail)


async def test_text():
    provider = openai_provider()
    delivery = Delivery()
    outcome = await AgentLoop().run(
        context(
            InMemoryEventStore(), provider, registry(), delivery, "只回复 NEXTLOOP_OK，不要解释。"
        )
    )
    assert outcome.status == "succeeded", outcome
    assert "NEXTLOOP_OK" in delivery.messages[0].content
    return passed("OpenAI 文本与 final 投递", delivery.messages[0].content[:100])


async def test_tool_round_trip():
    calls = []

    def read(arguments, ctx):
        calls.append(arguments["path"])
        return "NEXTLOOP_TOOL_FACT"

    provider = openai_provider()
    delivery = Delivery()
    outcome = await AgentLoop().run(
        context(
            InMemoryEventStore(),
            provider,
            registry(read),
            delivery,
            "必须实际调用一次工具 read(path='fact')，不能凭记忆或直接回答。"
            "拿到工具结果后回复 NEXTLOOP_TOOL_FACT。",
        )
    )
    assert outcome.status == "succeeded", outcome
    assert calls == ["fact"], (calls, outcome, delivery.messages)
    assert outcome.step >= 2
    return passed("Tool 调用与多 Step", f"steps={outcome.step}, calls={calls}")


async def test_parallel_tools():
    active = 0
    maximum = 0
    calls = []

    async def read(arguments, ctx):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        calls.append(arguments["path"])
        await asyncio.sleep(0.3)
        active -= 1
        return arguments["path"]

    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall("a", "read", '{"path":"a"}'),
                    ToolCall("b", "read", '{"path":"b"}'),
                ),
                finish_reason=FinishReason.TOOL_CALLS,
            ),
            ModelResponse(content="PARALLEL_OK"),
        ]
    )
    delivery = Delivery()
    prompt = "拿到两个独立 read 结果后回复 PARALLEL_OK。"
    outcome = await AgentLoop().run(
        context(InMemoryEventStore(), provider, registry(read), delivery, prompt)
    )
    assert outcome.status == "succeeded", outcome
    assert maximum == 2, (maximum, calls)
    assert len(calls) == 2, calls
    return passed("真实并行 Tool 调度", f"max_active={maximum}, calls={calls}")


async def test_exclusive_truncation():
    reads = []

    def write(arguments, ctx):
        return Completed("injected write failure", is_error=True)

    live = openai_provider()
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall("write-1", "write", "{}"),
                    ToolCall("read-later", "read", '{"path":"later"}'),
                ),
                finish_reason=FinishReason.TOOL_CALLS,
            )
        ],
        delegate=live,
    )
    delivery = Delivery()
    prompt = "write 失败。之后根据实际结果回复 EXCLUSIVE_HANDLED。"
    store = InMemoryEventStore()
    outcome = await AgentLoop().run(
        context(
            store,
            provider,
            registry(lambda arguments, ctx: reads.append(arguments), write),
            delivery,
            prompt,
        )
    )
    assert outcome.status == "succeeded", outcome
    assert reads == [], reads
    results = [
        event
        for event in await store.load_recovery("live")
        if event.event_type == EventType.TOOL_RESULT
    ]
    assert any(event.payload["outcome"]["kind"] == "skipped" for event in results)
    return passed("exclusive 失败截断", "后续 read 未执行且返回 Skipped")


async def test_unknown_recovery_and_release():
    started = 0

    async def write(arguments, ctx):
        nonlocal started
        started += 1
        await asyncio.sleep(1)
        return "unexpected"

    tools = registry(write_handler=write, write_timeout=0.05)
    tools.register(
        ToolSpec("release", "释放测试资源", {"type": "object"}),
        lambda arguments, ctx: Completed("released", release_resources=("workspace",)),
    )
    store = InMemoryEventStore()
    loop = AgentLoop()
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=(ToolCall("timeout-1", "write", "{}"),),
                finish_reason=FinishReason.TOOL_CALLS,
            )
        ],
        delegate=openai_provider(),
    )
    blocked = await loop.run(
        context(store, provider, tools, Delivery(), "只调用一次 write({})，不要调用其他工具。")
    )
    assert blocked.status == "blocked", blocked
    recovered = await loop.recover("live", store, Delivery())
    assert "workspace" in recovered.active_resource_locks
    before = started
    conflict_provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=(ToolCall("conflict", "write", "{}"),),
                finish_reason=FinishReason.TOOL_CALLS,
            )
        ]
    )
    conflict = await loop.run(
        context(
            store, conflict_provider, tools, Delivery(), "只调用一次 write({})，不要调用其他工具。"
        )
    )
    assert conflict.status == "blocked", conflict
    assert started == before, "冲突 Tool 被重复执行"
    release_calls = []

    def release(arguments, ctx):
        release_calls.append(arguments)
        return Completed("released", release_resources=("workspace",))

    tools._handlers["release"] = release
    release_provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=(ToolCall("release-1", "release", "{}"),),
                finish_reason=FinishReason.TOOL_CALLS,
            )
        ],
        delegate=provider,
    )
    released = await loop.run(
        context(
            store,
            release_provider,
            tools,
            Delivery(),
            "先调用 release({}) 释放资源，然后回复 RELEASE_OK。",
        )
    )
    assert released.status == "succeeded", released
    assert len(release_calls) == 1
    after_release = await loop.recover("live", store, Delivery())
    assert not after_release.active_resource_locks
    return passed("unknown、恢复、资源锁与释放", "未重放；冲突被阻止；release 后恢复")


async def test_steer():
    started = asyncio.Event()
    loop = AgentLoop()

    async def read(arguments, ctx):
        started.set()
        return "steer base fact"

    live = openai_provider()
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=(ToolCall("base", "read", '{"path":"base"}'),),
                finish_reason=FinishReason.TOOL_CALLS,
            )
        ],
        delegate=live,
    )
    delivery = Delivery()
    task = asyncio.create_task(
        loop.run(
            context(
                InMemoryEventStore(),
                provider,
                registry(read),
                delivery,
                "先调用一次 read(path='base')，然后简短回答。",
                session_id="steer",
            )
        )
    )
    await asyncio.wait_for(started.wait(), timeout=90)
    accepted = await loop.steer("steer", "追加要求：最终回复必须包含 STEER_OK")
    outcome = await task
    assert accepted is True
    assert outcome.status == "succeeded", outcome
    second_request = live.requests[-1]
    assert any("STEER_OK" in message.content for message in second_request.messages)
    return passed("运行中 steer", "在 Tool 边界注入，最终请求包含追加要求")


async def test_cancel():
    started = asyncio.Event()
    loop = AgentLoop()

    async def read(arguments, ctx):
        started.set()
        await ctx.cancel.wait()
        from nextloop import Cancelled

        return Cancelled(settled=True, partial_content="cancelled by smoke")

    task = asyncio.create_task(
        loop.run(
            context(
                InMemoryEventStore(),
                ScriptedProvider(
                    [
                        ModelResponse(
                            tool_calls=(ToolCall("cancel-1", "read", '{"path":"cancel"}'),),
                            finish_reason=FinishReason.TOOL_CALLS,
                        )
                    ]
                ),
                registry(read),
                Delivery(),
                "先调用 read(path='cancel') 并等待取消。",
                session_id="cancel",
            )
        )
    )
    await asyncio.wait_for(started.wait(), timeout=90)
    accepted = await loop.cancel("cancel")
    outcome = await task
    assert accepted is True
    assert outcome.status == "cancelled", outcome
    return passed("取消与统一 finalization", "Tool 收到取消，Turn 以 cancelled 结束")


async def test_compaction():
    provider = openai_provider()
    store = InMemoryEventStore()
    outcome = await AgentLoop().run(
        context(
            store,
            provider,
            registry(),
            Delivery(),
            "背景：" + "x" * 5000 + "\n请压缩当前任务后回复 COMPACTION_OK。",
            config=config(
                compaction_prompt="用一句中文短句概括当前任务，不调用工具。",
                context_window=1000,
                output_reserve=0,
            ),
        )
    )
    assert outcome.status == "succeeded", outcome
    events = await store.load_recovery("live")
    assert any(event.event_type == EventType.COMPACTION for event in events)
    assert provider.requests[0].messages[0].content.startswith("用一句中文短句")
    return passed("Compaction 与 epoch 投影", "先压缩再正常回复，事件包含 compaction")


async def test_protocol_correction():
    calls = []

    class InvalidOnce:
        def __init__(self):
            self.delegate = openai_provider()
            self.first = True

        async def complete(self, request, *, attempt, cancel):
            if self.first:
                self.first = False
                return ModelResponse(
                    tool_calls=(ToolCall("bad", "read", "{}"),),
                    finish_reason=FinishReason.TOOL_CALLS,
                )
            return await self.delegate.complete(request, attempt=attempt, cancel=cancel)

    provider = InvalidOnce()
    outcome = await AgentLoop().run(
        context(
            InMemoryEventStore(),
            provider,
            registry(lambda arguments, ctx: calls.append(arguments)),
            Delivery(),
            "修正上一轮缺少 path 的 Tool 调用后，直接回复 CORRECTION_OK。",
        )
    )
    assert outcome.status == "succeeded", outcome
    assert calls == []
    return passed("协议纠错", "首批零执行，纠错后由真实模型完成回复")


async def test_working_context_and_concludes_turn():
    tools = ToolRegistry()
    reminder = Message(
        role="user",
        content="[WorkingContext · turn]\n最终回复包含 CONTEXT_OK",
        provider_data={"working_context": {"action": "set", "scope": "turn"}},
    )
    tools.register(
        ToolSpec("ask_user", "设置提醒并向用户提问", {"type": "object"}),
        lambda arguments, ctx: Completed(
            "请选择 A 或 B", concludes_turn=True, additional_messages=(reminder,)
        ),
    )
    live = openai_provider()
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=(ToolCall("question-1", "ask_user", "{}"),),
                finish_reason=FinishReason.TOOL_CALLS,
            )
        ],
        delegate=live,
    )
    store = InMemoryEventStore()
    loop = AgentLoop()
    first = await loop.run(
        context(store, provider, tools, Delivery(), "调用 ask_user({}) 向我提问。")
    )
    assert first.status == "succeeded", first
    second = await loop.run(
        context(store, provider, tools, Delivery(), "选 B，并回复 CONTEXT_OK。")
    )
    assert second.status == "succeeded", second
    assert second.turn == 2
    assert any("CONTEXT_OK" in message.content for message in live.requests[-1].messages)
    return passed(
        "WorkingContext 与 concludes_turn", "提问结束 Turn 1，回答开启 Turn 2，提醒被注入"
    )


async def test_delivery_recovery():
    store = InMemoryEventStore()
    provider = openai_provider()
    delivery = Delivery(failures=1)
    loop = AgentLoop()
    outcome = await loop.run(
        context(store, provider, registry(), delivery, "只回复 DELIVERY_RECOVERY_OK。")
    )
    assert outcome.status == "delivery_unknown", outcome
    before = len(provider.requests)
    recovered = await loop.recover("live", store, delivery)
    assert recovered.delivered_final_ids == (outcome.final_message_id,)
    assert len(provider.requests) == before
    return passed("delivery-only recovery", "同一 final_message_id 重试，模型未重跑")


async def test_anthropic():
    provider = AnthropicProvider(
        model_id=MODEL,
        base_url=ANTHROPIC_URL,
        api_key=os.environ["KIMI_API_KEY"],
        default_parameters={"max_tokens": 512},
        timeout_seconds=90,
    )
    delivery = Delivery()
    outcome = await AgentLoop().run(
        context(
            InMemoryEventStore(), provider, registry(), delivery, "只回复 ANTHROPIC_OK，不要解释。"
        )
    )
    assert outcome.status == "succeeded", outcome
    assert "ANTHROPIC_OK" in delivery.messages[0].content
    return passed("Anthropic 协议", delivery.messages[0].content[:100])


async def main():
    scenarios = [
        test_text,
        test_tool_round_trip,
        test_parallel_tools,
        test_exclusive_truncation,
        test_unknown_recovery_and_release,
        test_steer,
        test_cancel,
        test_compaction,
        test_protocol_correction,
        test_working_context_and_concludes_turn,
        test_delivery_recovery,
        test_anthropic,
    ]
    results = []
    for scenario in scenarios:
        try:
            results.append(await scenario())
        except Exception as exc:
            print(f"FAIL {scenario.__name__}: {type(exc).__name__}: {exc}", flush=True)
            raise
    print(f"\n{len(results)}/{len(scenarios)} live scenarios passed", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
