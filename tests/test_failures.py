from __future__ import annotations

import asyncio
import os
import subprocess
import sys

import pytest
from helpers import FakeDelivery, FakeProvider
from test_loop import call, context, registry

from nextloop import (
    AgentLoop,
    Completed,
    EventType,
    FinishReason,
    InMemoryEventStore,
    ModelResponse,
    ProviderError,
    ToolCall,
    ToolRegistry,
    ToolSpec,
    TurnStatus,
    UncertainSettled,
)


@pytest.mark.asyncio
async def test_parallel_pool_never_exceeds_four_and_preserves_order():
    active = 0
    maximum = 0
    filled = asyncio.Event()
    release = asyncio.Event()

    async def read(arguments, ctx):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        if active == 4:
            filled.set()
        await release.wait()
        active -= 1
        return arguments["path"]

    tools = registry(read_handler=read)
    calls = tuple(call(call_id=f"c{index}", path=f"{index}.txt") for index in range(6))
    provider = FakeProvider(
        [
            ModelResponse(tool_calls=calls, finish_reason=FinishReason.TOOL_CALLS),
            ModelResponse(content="done"),
        ]
    )
    store = InMemoryEventStore()
    task = asyncio.create_task(AgentLoop().run(context(store, provider, tools, FakeDelivery())))
    await asyncio.wait_for(filled.wait(), timeout=1)
    assert maximum == 4
    release.set()
    outcome = await task
    assert outcome.status == TurnStatus.SUCCEEDED
    results = [
        event.payload["message"]["content"]
        for event in await store.load_recovery("s")
        if event.event_type == EventType.TOOL_RESULT
    ]
    assert results == [f"{index}.txt" for index in range(6)]


@pytest.mark.asyncio
async def test_parallel_unknown_closes_later_exclusive_work():
    reads = []

    async def read(arguments, ctx):
        reads.append(arguments["path"])
        if arguments["path"] == "uncertain":
            return UncertainSettled("read timed out after exit")
        return "ok"

    writes = []
    tools = registry(
        read_handler=read, write_handler=lambda arguments, ctx: writes.append(arguments)
    )
    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=(
                    call(call_id="a", path="uncertain"),
                    call(call_id="b", path="other"),
                    call("write", "w"),
                ),
                finish_reason=FinishReason.TOOL_CALLS,
            ),
            ModelResponse(content="inspect first"),
        ]
    )
    outcome = await AgentLoop().run(context(InMemoryEventStore(), provider, tools, FakeDelivery()))
    assert outcome.status == TurnStatus.SUCCEEDED
    assert reads == ["uncertain", "other"]
    assert writes == []


@pytest.mark.asyncio
async def test_protocol_exhaustion_and_partial_failure_stop_cleanly():
    exhausted = await AgentLoop().run(
        context(
            InMemoryEventStore(),
            FakeProvider([ModelResponse(), ModelResponse(), ModelResponse()]),
            registry(),
            FakeDelivery(),
        )
    )
    assert exhausted.status == TurnStatus.FAILED
    assert "ProtocolExhausted" in exhausted.error

    partial = await AgentLoop().run(
        context(
            InMemoryEventStore(),
            FakeProvider([ProviderError("disconnected", retryable=True, partial_output=True)]),
            registry(),
            FakeDelivery(),
        )
    )
    assert partial.status == TurnStatus.FAILED


@pytest.mark.asyncio
async def test_steer_during_retry_creates_new_step_not_exact_retry():
    loop = AgentLoop()

    async def fail_with_steer(**kwargs):
        assert await loop.steer("s", "new requirement")
        raise ProviderError("temporary", retryable=True)

    provider = FakeProvider([fail_with_steer, ModelResponse(content="done")])
    outcome = await loop.run(context(InMemoryEventStore(), provider, registry(), FakeDelivery()))
    assert outcome.status == TurnStatus.SUCCEEDED
    assert outcome.step == 2
    assert [attempt for attempt, _ in provider.attempts] == [1, 1]
    assert provider.requests[1].messages[-1].content == "new requirement"


@pytest.mark.asyncio
async def test_second_turn_continues_session_sequence():
    store = InMemoryEventStore()
    loop = AgentLoop()
    first = await loop.run(
        context(store, FakeProvider([ModelResponse(content="one")]), registry(), FakeDelivery())
    )
    second = await loop.run(
        context(store, FakeProvider([ModelResponse(content="two")]), registry(), FakeDelivery())
    )
    assert (first.turn, second.turn) == (1, 2)
    events = await store.load_recovery("s")
    assert [event.seq for event in events] == list(range(1, len(events) + 1))


def run_crash_script(path, mode):
    script = f"""
import asyncio
import os
from helpers import FakeDelivery, FakeProvider
from nextloop import AgentLoop, ModelResponse, FinishReason
from test_loop import call, context, registry
from nextloop.adapters.sqlite import SQLiteEventStore

async def main():
    store = SQLiteEventStore({str(path)!r})
    tools = registry(write_handler=lambda arguments, ctx: os._exit(23))
    provider = FakeProvider([
        ModelResponse(tool_calls=(call('write'),), finish_reason=FinishReason.TOOL_CALLS)
    ])
    delivery = FakeDelivery()
    if {mode!r} == 'delivery':
        provider = FakeProvider([ModelResponse(content='committed answer')])
        class CrashDelivery:
            async def deliver(self, message):
                os._exit(24)
        delivery = CrashDelivery()
    await AgentLoop().run(context(store, provider, tools, delivery))

asyncio.run(main())
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = "src:tests"
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=os.path.dirname(os.path.dirname(__file__)),
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


@pytest.mark.asyncio
async def test_real_process_exit_after_tool_start_recovers_sqlite(tmp_path):
    path = tmp_path / "crash.sqlite3"
    result = run_crash_script(path, "tool")
    assert result.returncode == 23, result.stderr
    from nextloop.adapters.sqlite import SQLiteEventStore

    store = SQLiteEventStore(str(path))
    recovered = await AgentLoop().recover("s", store, FakeDelivery())
    assert recovered.interrupted_turns == (1,)
    assert recovered.active_resource_locks == frozenset({"workspace"})
    events = await store.load_recovery("s")
    assert any(
        event.event_type == EventType.TOOL_RESULT
        and event.payload["outcome"]["kind"] == "uncertain_hanging"
        for event in events
    )

    release_tools = ToolRegistry()
    release_tools.register(
        ToolSpec("release", "Verify and release", {"type": "object"}),
        lambda arguments, ctx: Completed("released", release_resources=("workspace",)),
    )
    loop = AgentLoop()
    recovered_loop = await loop.recover("s", store, FakeDelivery())
    assert recovered_loop.active_resource_locks == frozenset({"workspace"})
    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=(ToolCall("r", "release", "{}"),),
                finish_reason=FinishReason.TOOL_CALLS,
            ),
            ModelResponse(content="released"),
        ]
    )
    outcome = await loop.run(context(store, provider, release_tools, FakeDelivery()))
    assert outcome.status == TurnStatus.SUCCEEDED
    store.close()


@pytest.mark.asyncio
async def test_real_process_exit_after_final_commit_only_recovers_delivery(tmp_path):
    path = tmp_path / "delivery.sqlite3"
    result = run_crash_script(path, "delivery")
    assert result.returncode == 24, result.stderr
    from nextloop.adapters.sqlite import SQLiteEventStore

    store = SQLiteEventStore(str(path))
    delivery = FakeDelivery()
    recovered = await AgentLoop().recover("s", store, delivery)
    assert len(recovered.delivered_final_ids) == 1
    assert delivery.messages[0].content == "committed answer"
    store.close()


class ConflictingCrash(BaseException):
    pass


@pytest.mark.asyncio
async def test_recovered_lock_prevents_conflicting_tool_dispatch():
    invocations = []

    def write(arguments, ctx):
        invocations.append(arguments)
        raise ConflictingCrash()

    tools = registry(write_handler=write)
    store = InMemoryEventStore()
    loop = AgentLoop()
    provider = FakeProvider(
        [ModelResponse(tool_calls=(call("write"),), finish_reason=FinishReason.TOOL_CALLS)]
    )
    with pytest.raises(ConflictingCrash):
        await loop.run(context(store, provider, tools, FakeDelivery()))
    await loop.recover("s", store, FakeDelivery())

    blocked_provider = FakeProvider(
        [ModelResponse(tool_calls=(call("write"),), finish_reason=FinishReason.TOOL_CALLS)]
    )
    outcome = await loop.run(context(store, blocked_provider, tools, FakeDelivery()))
    assert outcome.status == TurnStatus.BLOCKED
    assert len(invocations) == 1
    assert len(blocked_provider.requests) == 1
