from __future__ import annotations

import pytest
from projection_benchmark import run_benchmark


@pytest.mark.asyncio
async def test_10k_projection_parity(tmp_path):
    report = await run_benchmark(tmp_path)

    assert report["events"] == 10_505
    assert report["surface_events"] == 1_502
    assert report["trace_records"] == 3_001
    assert report["memory_sqlite_projection_parity"] is True
    assert report["memory_sqlite_trace_payload_parity"] is True
    assert report["incremental_trace_parity"] is True
    assert report["required_content_preserved"] is True
