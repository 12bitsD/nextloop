from __future__ import annotations

import asyncio
import multiprocessing as mp

import pytest
from helpers import FakeDelivery, FakeProvider, config
from test_approval import approval_tools
from test_loop import call, context, registry

from nextloop import (
    AgentLoop,
    Completed,
    EventType,
    FinishReason,
    InMemoryEventStore,
    InvariantViolation,
    ModelResponse,
    NewEvent,
    SQLiteEventStore,
    SteerStatus,
    WriterLeaseError,
)


class Clock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value


@pytest.fixture(params=["memory", "sqlite"])
def stores(request, tmp_path):
    clock = Clock()
    if request.param == "memory":

        def factory(owner):
            return InMemoryEventStore(owner_id=owner, lease_seconds=10, clock=clock)

    else:

        def factory(owner):
            return SQLiteEventStore(
                str(tmp_path / "lease.sqlite3"), owner_id=owner, lease_seconds=10, clock=clock
            )

    return factory, clock, request.param


@pytest.mark.asyncio
async def test_same_owner_renews_and_immediately_takes_over(stores):
    factory, clock, _ = stores
    store = factory("owner-a")
    first, _, _ = await store.attach_writer("s")
    second, _, _ = await store.attach_writer("s")

    assert second > first
    with pytest.raises(InvariantViolation):
        await store.check_writer("s", first)
    await store.check_writer("s", second)

    clock.value += 5
    await store.renew_writer("s", second)
    clock.value += 6
    await store.check_writer("s", second)
    clock.value += 11
    with pytest.raises(InvariantViolation):
        await store.check_writer("s", second)
    with pytest.raises(InvariantViolation):
        await store.renew_writer("s", second)

    third, _, _ = await store.attach_writer("s")
    assert third > second


@pytest.mark.asyncio
async def test_different_owner_waits_for_expiry_or_forces_takeover(stores):
    factory, clock, backend = stores
    if backend == "memory":
        pytest.skip("Cross-owner takeover requires a shared Store")
    first = factory("owner-a")
    second = factory("owner-b")
    first_epoch, _, _ = await first.attach_writer("s")

    with pytest.raises(WriterLeaseError):
        await second.attach_writer("s")

    clock.value += 11
    second_epoch, _, _ = await second.attach_writer("s")
    assert second_epoch > first_epoch
    with pytest.raises(InvariantViolation):
        await first.check_writer("s", first_epoch)

    third = factory("owner-c")
    with pytest.raises(WriterLeaseError):
        await third.attach_writer("s")
    third_epoch, _, _ = await third.attach_writer("s", force=True)
    assert third_epoch > second_epoch
    with pytest.raises(InvariantViolation):
        await second.check_writer("s", second_epoch)


@pytest.mark.asyncio
async def test_legacy_attach_without_owner_keeps_epoch_semantics():
    store = InMemoryEventStore()
    first, _, _ = await store.attach_writer("s")
    second, _, _ = await store.attach_writer("s")

    assert (first, second) == (1, 2)
    with pytest.raises(InvariantViolation):
        await store.check_writer("s", first)
    await store.check_writer("s", second)


@pytest.mark.asyncio
async def test_heartbeat_renews_lease_during_long_tool():
    store = InMemoryEventStore(owner_id="owner", lease_seconds=0.03)

    async def write(arguments, ctx):
        await asyncio.sleep(0.08)
        return Completed("done")

    provider = FakeProvider(
        [
            ModelResponse(tool_calls=(call("write"),), finish_reason=FinishReason.TOOL_CALLS),
            ModelResponse(content="done"),
        ]
    )
    outcome = await AgentLoop().run(
        context(store, provider, registry(write_handler=write), FakeDelivery())
    )

    assert outcome.status == "succeeded"
    assert await store.has_writer("s")


@pytest.mark.asyncio
async def test_expired_writer_cannot_dispatch_after_resume():
    clock = Clock()

    class PauseStore(InMemoryEventStore):
        def __init__(self):
            super().__init__(owner_id="owner", lease_seconds=10, clock=clock)
            self.committed = asyncio.Event()
            self.resume = asyncio.Event()

        async def renew_writer(self, session_id, writer_epoch):
            return None

        async def append(self, session_id, writer_epoch, expected_seq, events):
            committed = await super().append(session_id, writer_epoch, expected_seq, events)
            if any(event.event_type == EventType.TOOL_CALL for event in committed):
                self.committed.set()
                await self.resume.wait()
            return committed

    store = PauseStore()
    invoked = []
    provider = FakeProvider(
        [ModelResponse(tool_calls=(call("write"),), finish_reason=FinishReason.TOOL_CALLS)]
    )
    task = asyncio.create_task(
        AgentLoop().run(
            context(
                store,
                provider,
                registry(write_handler=lambda a, c: invoked.append(a)),
                FakeDelivery(),
            )
        )
    )
    await asyncio.wait_for(store.committed.wait(), timeout=1)
    clock.value += 11
    store.resume.set()

    with pytest.raises(InvariantViolation):
        await task
    assert invoked == []


@pytest.mark.asyncio
async def test_steer_result_distinguishes_input_rejections():
    loop = AgentLoop()
    store = InMemoryEventStore()
    assert (await loop.steer_result("s", "missing")).status == SteerStatus.SESSION_MISSING
    assert (await loop.steer_result("s", "bad", input_id="")).status == SteerStatus.INVALID_INPUT

    started = asyncio.Event()
    release = asyncio.Event()

    async def respond(**kwargs):
        started.set()
        await release.wait()
        return ModelResponse(content="first")

    provider = FakeProvider([respond, ModelResponse(content="combined")])
    task = asyncio.create_task(
        loop.run(
            context(
                store,
                provider,
                registry(),
                FakeDelivery(),
                config=config(max_pending_inputs=1),
            )
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    assert (await loop.steer_result("s", "first")).status == SteerStatus.ACCEPTED
    assert (await loop.steer_result("s", "second")).status == SteerStatus.QUEUE_FULL
    release.set()
    await task
    assert (await loop.steer_result("s", "closed")).status == SteerStatus.TURN_CLOSED


@pytest.mark.asyncio
async def test_non_writer_is_rejected_and_force_recover_takes_over(tmp_path):
    path = str(tmp_path / "shared.sqlite3")
    first = SQLiteEventStore(path, owner_id="owner-a", lease_seconds=10)
    second = SQLiteEventStore(path, owner_id="owner-b", lease_seconds=10)
    started = asyncio.Event()
    release = asyncio.Event()

    async def respond(**kwargs):
        started.set()
        await release.wait()
        return ModelResponse(content="first")

    provider = FakeProvider([respond, ModelResponse(content="done")])
    task = asyncio.create_task(
        AgentLoop().run(context(first, provider, registry(), FakeDelivery()))
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    other = AgentLoop()
    with pytest.raises(WriterLeaseError):
        await other.recover("s", second, FakeDelivery())
    assert (await other.steer_result("s", "not mine")).status == SteerStatus.NOT_WRITER
    release.set()
    await task

    recovered = await other.recover("s", second, FakeDelivery(), force=True)
    assert recovered.writer_epoch == 2
    first.close()
    second.close()


@pytest.mark.asyncio
async def test_late_tool_result_is_discarded_after_lease_takeover(tmp_path):
    path = str(tmp_path / "late.sqlite3")
    first = SQLiteEventStore(path, owner_id="owner-a", lease_seconds=10)
    second = SQLiteEventStore(path, owner_id="owner-b", lease_seconds=10)
    started = asyncio.Event()
    release = asyncio.Event()
    invoked = []

    async def write(arguments, ctx):
        invoked.append(arguments)
        started.set()
        await release.wait()
        return Completed("late")

    provider = FakeProvider(
        [ModelResponse(tool_calls=(call("write"),), finish_reason=FinishReason.TOOL_CALLS)]
    )
    task = asyncio.create_task(
        AgentLoop().run(context(first, provider, registry(write_handler=write), FakeDelivery()))
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    await second.attach_writer("s", force=True)
    release.set()

    with pytest.raises(InvariantViolation):
        await task
    assert len(invoked) == 1
    before_recovery = await second.load_recovery("s")
    assert not any(event.event_type == EventType.TOOL_RESULT for event in before_recovery)
    recovered = await AgentLoop().recover("s", second, FakeDelivery())
    assert recovered.active_resource_locks == frozenset({"workspace"})
    recovered_result = next(
        event
        for event in await second.load_recovery("s")
        if event.event_type == EventType.TOOL_RESULT
    )
    assert recovered_result.payload["outcome"]["kind"] == "uncertain_hanging"
    first.close()
    second.close()


@pytest.mark.asyncio
async def test_cancel_requires_current_lease_owner(tmp_path):
    path = str(tmp_path / "cancel.sqlite3")
    first = SQLiteEventStore(path, owner_id="owner-a", lease_seconds=10)
    second = SQLiteEventStore(path, owner_id="owner-b", lease_seconds=10)
    invoked = []
    tools = approval_tools(invoked)
    provider = FakeProvider(
        [ModelResponse(tool_calls=(call("write"),), finish_reason=FinishReason.TOOL_CALLS)]
    )
    loop = AgentLoop()
    await loop.run(context(first, provider, tools, FakeDelivery()))
    other = AgentLoop()
    with pytest.raises(WriterLeaseError):
        await other.recover("s", second, FakeDelivery())

    assert not await other.cancel("s")
    assert await loop.cancel("s")
    assert invoked == []
    first.close()
    second.close()


@pytest.mark.asyncio
async def test_legacy_takeover_invalidates_lease_owner(tmp_path):
    path = str(tmp_path / "legacy.sqlite3")
    leased = SQLiteEventStore(path, owner_id="owner-a", lease_seconds=10)
    legacy = SQLiteEventStore(path)
    epoch, _, _ = await leased.attach_writer("s")
    legacy_epoch, _, _ = await legacy.attach_writer("s")

    assert legacy_epoch > epoch
    with pytest.raises(InvariantViolation):
        await leased.check_writer("s", epoch)
    assert not await leased.has_writer("s")
    with pytest.raises(InvariantViolation):
        await leased.append("s", epoch, 1, [NewEvent(EventType.TURN_START, turn=1, step=0)])
    leased.close()
    legacy.close()


@pytest.mark.asyncio
async def test_operational_heartbeat_failure_cancels_execution():
    class BrokenRenewStore(InMemoryEventStore):
        def __init__(self):
            super().__init__(owner_id="owner", lease_seconds=0.01)

        async def renew_writer(self, session_id, writer_epoch):
            raise RuntimeError("renewal transport failed")

    store = BrokenRenewStore()

    async def write(arguments, ctx):
        await asyncio.sleep(0.1)
        return Completed("unreachable")

    provider = FakeProvider(
        [
            ModelResponse(tool_calls=(call("write"),), finish_reason=FinishReason.TOOL_CALLS),
            ModelResponse(content="unreachable"),
        ]
    )
    outcome = await AgentLoop().run(
        context(store, provider, registry(write_handler=write), FakeDelivery())
    )

    assert outcome.status == "cancelled"
    assert not any(
        event.event_type == EventType.CANCELLATION_REQUESTED
        for event in await store.load_recovery("s")
    )


@pytest.mark.asyncio
async def test_lease_concurrent_resume_and_repeated_recover(tmp_path):
    path = str(tmp_path / "resume.sqlite3")
    first = SQLiteEventStore(path, owner_id="owner-a", lease_seconds=10)
    second = SQLiteEventStore(path, owner_id="owner-a", lease_seconds=10)
    invoked = []
    tools = approval_tools(invoked)
    provider = FakeProvider(
        [
            ModelResponse(tool_calls=(call("write"),), finish_reason=FinishReason.TOOL_CALLS),
            ModelResponse(content="done"),
        ]
    )
    loop = AgentLoop()
    await loop.run(context(first, provider, tools, FakeDelivery()))
    operation_id = next(
        event.payload["items"][0]["operation_id"]
        for event in await first.load_recovery("s")
        if event.event_type == EventType.APPROVAL_REQUESTED
    )
    first_loop = AgentLoop()
    second_loop = AgentLoop()

    outcomes = await asyncio.gather(
        first_loop.resume(
            context(first, provider, tools, FakeDelivery()), decisions={operation_id: True}
        ),
        second_loop.resume(
            context(second, provider, tools, FakeDelivery()), decisions={operation_id: True}
        ),
        return_exceptions=True,
    )

    assert sum(isinstance(outcome, Exception) for outcome in outcomes) == 1
    assert sum(not isinstance(outcome, Exception) for outcome in outcomes) == 1
    assert len(invoked) == 1
    recovered = await second_loop.recover("s", second, FakeDelivery())
    assert recovered.interrupted_turns == ()
    repeated = await second_loop.recover("s", second, FakeDelivery())
    assert repeated.interrupted_turns == ()
    assert (
        sum(
            event.event_type == EventType.APPROVAL_REQUESTED
            for event in await second.load_recovery("s")
        )
        == 1
    )
    first.close()
    second.close()


@pytest.mark.asyncio
async def test_renewal_cannot_succeed_after_expiry(tmp_path):
    clock = Clock()

    class ExpiringStore(SQLiteEventStore):
        def _check_writer_locked(self, session_id, writer_epoch):
            super()._check_writer_locked(session_id, writer_epoch)
            clock.value += 11

    store = ExpiringStore(
        str(tmp_path / "renew.sqlite3"), owner_id="owner-a", lease_seconds=10, clock=clock
    )
    epoch, _, _ = await store.attach_writer("s")

    with pytest.raises(InvariantViolation):
        await store.renew_writer("s", epoch)
    assert not await store.has_writer("s")
    store.close()


def lease_worker(path, owner, start, results):
    store = SQLiteEventStore(path, owner_id=owner, lease_seconds=10)
    try:
        start.wait()
        epoch, _, _ = asyncio.run(store.attach_writer("s"))
        results.put(("ok", owner, epoch))
    except WriterLeaseError:
        results.put(("conflict", owner, 0))
    finally:
        store.close()


def test_same_host_multiprocess_lease_contention(tmp_path):
    path = str(tmp_path / "process.sqlite3")
    context = mp.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(target=lease_worker, args=(path, owner, start, results))
        for owner in ("owner-a", "owner-b")
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=10)

    assert all(process.exitcode == 0 for process in processes)
    assert sorted(results.get(timeout=2)[0] for _ in processes) == ["conflict", "ok"]
