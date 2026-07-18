from __future__ import annotations

import json
import time
import tracemalloc
from pathlib import Path

import pytest

from sidecar.stats import MAX_TOOLS, RECENT_BUCKET_COUNT, StatsStore


@pytest.mark.asyncio
async def test_stats_store_aggregates_100k_calls_with_bounded_storage(
    tmp_path: Path,
) -> None:
    path = tmp_path / "stats.json"
    tracemalloc.start()
    store = StatsStore(path)

    for index in range(100_000):
        success = index % 10 != 0
        store.record_operation(
            "execute",
            duration_ms=float(index % 100),
            success=success,
            failure_stage=None if success else "runtime",
            input_bytes=10,
            output_bytes=20,
            calls=2,
            chain_calls=1,
        )
        store.record_upstream(
            f"server-{index % 100}",
            f"tool-{index % 1_000}",
            duration_ms=float(index % 50),
            success=success,
            input_bytes=5,
            output_bytes=15,
        )

    await store.flush()
    snapshot = store.snapshot()
    lifetime = snapshot["lifetime"]
    assert isinstance(lifetime, dict)
    assert lifetime["count"] == 100_000
    assert lifetime["success"] == 90_000
    assert lifetime["failure"] == 10_000
    assert len(store.tools) <= MAX_TOOLS
    assert len(store.recent) <= RECENT_BUCKET_COUNT
    assert path.stat().st_size < 250_000
    current_memory, peak_memory = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert current_memory < 10_000_000
    assert peak_memory < 20_000_000

    serialized = path.read_text(encoding="utf-8")
    assert "arguments" not in serialized
    assert "result" not in serialized
    assert "query" not in serialized
    assert "code" not in serialized

    load_started = time.perf_counter()
    restored = StatsStore(path)
    load_seconds = time.perf_counter() - load_started
    restored_snapshot = restored.snapshot()
    restored_lifetime = restored_snapshot["lifetime"]
    assert isinstance(restored_lifetime, dict)
    assert restored_lifetime["count"] == 100_000
    assert len(restored.tools) <= MAX_TOOLS
    assert load_seconds < 1.0
    await restored.close()


def test_stats_file_is_compact_rollup_not_event_log(tmp_path: Path) -> None:
    path = tmp_path / "stats.json"
    store = StatsStore(path)
    for _ in range(10):
        store.record_operation("search", duration_ms=2, success=True)
    payload = store.snapshot()

    assert "events" not in payload
    assert json.dumps(payload).count('"search"') == 1
