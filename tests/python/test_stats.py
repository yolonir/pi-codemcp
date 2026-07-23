from __future__ import annotations

import asyncio
import multiprocessing
import sqlite3
import time
import tracemalloc
from pathlib import Path

import pytest

from sidecar.json_types import JsonObject
from sidecar.stats import (
    MAX_TOOLS,
    RECENT_BUCKET_COUNT,
    RECENT_FAILURE_LIMIT,
    OperationFailure,
    OperationObservation,
    StatsStore,
)


def required_object(record: JsonObject, key: str) -> JsonObject:
    value = record[key]
    assert isinstance(value, dict)
    return value


def required_integer(record: JsonObject, key: str) -> int:
    value = record[key]
    assert isinstance(value, int) and not isinstance(value, bool)
    return value


def _record_stats_process(path: str, process_index: int, observations: int) -> None:
    async def run() -> None:
        store = StatsStore(Path(path), package_version="process-test")
        for observation_index in range(observations):
            failed = observation_index % 5 == 0
            trace_id = f"process:{process_index}:{observation_index}"
            store.record_operation(
                "execute",
                OperationObservation(
                    duration_ms=float(observation_index),
                    success=not failed,
                    input_bytes=10,
                    output_bytes=20,
                    calls=2,
                    chain_calls=1,
                    failure=OperationFailure(
                        stage="runtime",
                        subtype="upstream_runtime",
                        trace_id=trace_id,
                        server="grafana",
                        tool="query_prometheus",
                    )
                    if failed
                    else None,
                ),
            )
            store.record_phase("execution", float(observation_index))
            store.record_upstream(
                "grafana",
                "query_prometheus",
                duration_ms=float(observation_index),
                success=not failed,
                input_bytes=5,
                output_bytes=15 if not failed else 0,
            )
            store.record_cache(hit=observation_index % 2 == 0)
        await store.close()

    asyncio.run(run())


@pytest.mark.asyncio
async def test_stats_store_aggregates_100k_calls_with_bounded_storage(
    tmp_path: Path,
) -> None:
    path = tmp_path / "stats.sqlite3"
    tracemalloc.start()
    store = StatsStore(path, package_version="1.2.3")

    for index in range(100_000):
        success = index % 10 != 0
        store.record_operation(
            "execute",
            OperationObservation(
                duration_ms=float(index % 100),
                success=success,
                input_bytes=10,
                output_bytes=20,
                calls=2,
                chain_calls=1,
                failure=None
                if success
                else OperationFailure(
                    stage="runtime",
                    subtype="upstream_runtime",
                    trace_id=f"call:{index}",
                    server="server",
                    tool="tool",
                ),
            ),
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
    snapshot = await store.snapshot()
    assert isinstance(snapshot["updated_at"], int) and snapshot["updated_at"] > 0
    lifetime = snapshot["lifetime"]
    assert isinstance(lifetime, dict)
    assert lifetime["count"] == 100_000
    assert lifetime["success"] == 90_000
    assert lifetime["failure"] == 10_000
    tools = snapshot["tools"]
    recent = snapshot["recent"]
    failures = snapshot["recent_failures"]
    outcomes = snapshot["outcomes"]
    assert isinstance(tools, dict) and len(tools) <= MAX_TOOLS
    assert isinstance(recent, list) and len(recent) <= RECENT_BUCKET_COUNT
    assert isinstance(failures, list) and len(failures) == RECENT_FAILURE_LIMIT
    latest_failure = failures[0]
    assert isinstance(latest_failure, dict)
    assert latest_failure["package_version"] == "1.2.3"
    assert isinstance(outcomes, dict)
    assert outcomes == {"success": 90_000, "upstream_failure": 10_000}
    assert path.read_bytes().startswith(b"SQLite format 3\x00")
    assert path.stat().st_size < 500_000
    current_memory, peak_memory = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert current_memory < 10_000_000
    assert peak_memory < 20_000_000

    load_started = time.perf_counter()
    restored = StatsStore(path)
    restored_snapshot = await restored.snapshot()
    load_seconds = time.perf_counter() - load_started
    assert restored_snapshot["updated_at"] == snapshot["updated_at"]
    restored_lifetime = restored_snapshot["lifetime"]
    assert isinstance(restored_lifetime, dict)
    assert restored_lifetime["count"] == 100_000
    restored_tools = restored_snapshot["tools"]
    assert isinstance(restored_tools, dict) and len(restored_tools) <= MAX_TOOLS
    assert load_seconds < 1.0
    await restored.close()
    await store.close()


@pytest.mark.asyncio
async def test_failure_references_are_bounded_and_payload_free(tmp_path: Path) -> None:
    path = tmp_path / "stats.sqlite3"
    store = StatsStore(path, package_version="9.9.9")
    secret_values = {
        "code": "SECRET_CODE_PAYLOAD",
        "arguments": "SECRET_ARGUMENT_PAYLOAD",
        "query": "SECRET_QUERY_PAYLOAD",
        "result": "SECRET_RESULT_PAYLOAD",
        "error": "SECRET_ERROR_PAYLOAD",
        "credential": "SECRET_CREDENTIAL_PAYLOAD",
    }

    for index in range(RECENT_FAILURE_LIMIT + 50):
        store.record_operation(
            "execute",
            OperationObservation(
                duration_ms=2,
                success=False,
                calls=1,
                failure=OperationFailure(
                    stage="preflight",
                    subtype="preflight_typecheck",
                    trace_id=f"trace:{index}",
                ),
            ),
        )
    await store.flush()
    snapshot = await store.snapshot()

    recent_failures = snapshot["recent_failures"]
    assert isinstance(recent_failures, list)
    assert len(recent_failures) == RECENT_FAILURE_LIMIT
    assert all(
        isinstance(item, dict) and "trace_id" in item for item in recent_failures
    )
    latest_failure = recent_failures[0]
    assert isinstance(latest_failure, dict)
    assert latest_failure["trace_id"] == f"trace:{RECENT_FAILURE_LIMIT + 49}"

    connection = sqlite3.connect(path)
    try:
        dump = "\n".join(connection.iterdump())
        failure_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(failure_events)")
        }
    finally:
        connection.close()
    assert failure_columns == {
        "id",
        "timestamp",
        "trace_id",
        "operation",
        "stage",
        "subtype",
        "calls",
        "chain_calls",
        "server",
        "tool",
        "package_version",
    }
    serialized_snapshot = str(snapshot)
    for secret in secret_values.values():
        assert secret not in dump
        assert secret not in serialized_snapshot
    await store.close()


def test_concurrent_processes_merge_exact_monotonic_rollups(tmp_path: Path) -> None:
    path = tmp_path / "stats.sqlite3"
    process_count = 10
    observations = 20
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(
            target=_record_stats_process,
            args=(str(path), process_index, observations),
        )
        for process_index in range(process_count)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0

    snapshot = asyncio.run(StatsStore(path).snapshot())
    expected = process_count * observations
    expected_failures = process_count * (observations // 5)
    lifetime = snapshot["lifetime"]
    operations = snapshot["operations"]
    phases = snapshot["phases"]
    servers = snapshot["servers"]
    tools = snapshot["tools"]
    cache = snapshot["cache"]
    failures = snapshot["failures"]
    outcomes = snapshot["outcomes"]
    assert isinstance(lifetime, dict) and lifetime["count"] == expected
    assert isinstance(operations, dict)
    operation = required_object(operations, "execute")
    assert operation["count"] == expected
    assert required_object(operation, "duration_ms")["count"] == expected
    assert isinstance(phases, dict)
    assert required_object(phases, "execution")["count"] == expected
    assert isinstance(servers, dict)
    server = required_object(servers, "grafana")
    assert server["count"] == expected
    assert required_object(server, "duration_ms")["count"] == expected
    assert isinstance(tools, dict)
    tool = required_object(tools, "grafana.query_prometheus")
    assert tool["count"] == expected
    connection = sqlite3.connect(path)
    try:
        tool_histogram_count = connection.execute(
            """
            SELECT SUM(count) FROM histograms
            WHERE dimension = 'tool'
              AND name = 'grafana.query_prometheus'
              AND metric = 'duration_ms'
            """
        ).fetchone()
    finally:
        connection.close()
    assert tool_histogram_count == (expected,)
    assert isinstance(cache, dict)
    assert required_integer(cache, "hits") == expected // 2
    assert required_integer(cache, "misses") == expected // 2
    assert isinstance(failures, dict)
    assert failures == {"runtime": expected_failures}
    assert isinstance(outcomes, dict)
    assert outcomes == {
        "success": expected - expected_failures,
        "upstream_failure": expected_failures,
    }
    assert lifetime["failure"] == expected_failures

    stale_writer = StatsStore(path, package_version="stale")
    fresh_writer = StatsStore(path, package_version="fresh")
    stale_writer.record_operation(
        "execute",
        OperationObservation(duration_ms=1, success=True),
    )
    fresh_writer.record_operation(
        "execute",
        OperationObservation(duration_ms=1, success=True),
    )
    asyncio.run(fresh_writer.flush())
    after_fresh = asyncio.run(fresh_writer.snapshot())
    asyncio.run(stale_writer.flush())
    after_stale = asyncio.run(stale_writer.snapshot())
    fresh_lifetime = after_fresh["lifetime"]
    stale_lifetime = after_stale["lifetime"]
    assert isinstance(fresh_lifetime, dict)
    assert isinstance(stale_lifetime, dict)
    assert fresh_lifetime["count"] == expected + 1
    assert stale_lifetime["count"] == expected + 2
