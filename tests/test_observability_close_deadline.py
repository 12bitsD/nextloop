from __future__ import annotations

import asyncio

import pytest

from nextloop import InMemoryEventStore, ObservingEventStore


class HangingCloseExporter:
    def __init__(self):
        self.flushed = False

    def export(self, trace):
        pass

    def flush(self):
        self.flushed = True

    async def close(self):
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_exporter_close_is_bounded_and_can_be_retried():
    exporter = HangingCloseExporter()
    store = ObservingEventStore(
        InMemoryEventStore(),
        [exporter],
        close_exporters=[exporter],
        close_timeout=0.02,
    )

    await asyncio.wait_for(store.close(), timeout=0.2)
    assert not store._closed


@pytest.mark.asyncio
async def test_cancellation_during_exporter_close_propagates():
    exporter = HangingCloseExporter()
    store = ObservingEventStore(
        InMemoryEventStore(),
        [exporter],
        close_exporters=[exporter],
        close_timeout=0.2,
    )
    closing = asyncio.create_task(store.close())
    await asyncio.sleep(0.01)
    closing.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(closing, timeout=0.5)
    assert not store._closed
