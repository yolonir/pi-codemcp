from __future__ import annotations

import asyncio
import sqlite3
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from .json_types import JSON_OBJECT_ADAPTER, JSON_VALUE_ADAPTER, JsonObject

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

HISTOGRAM_BOUNDS_MS = (1, 5, 10, 25, 50, 100, 250, 500, 1_000, 2_500, 5_000, 10_000, 30_000)
HISTOGRAM_BOUNDS_BYTES = (64, 256, 1_024, 4_096, 16_384, 65_536, 262_144, 1_048_576)
RECENT_BUCKET_SECONDS = 60 * 60
RECENT_BUCKET_COUNT = 24
RECENT_FAILURE_LIMIT = 200
FLUSH_DELAY_SECONDS = 5.0
MAX_OPERATIONS = 32
MAX_PHASES = 16
MAX_SERVERS = 64
MAX_TOOLS = 384
MAX_FAILURE_STAGES = 16
MAX_OUTCOMES = 16
OTHER_DIMENSION = "<other>"
SQLITE_SCHEMA_VERSION = 1
SQLITE_BUSY_TIMEOUT_MS = 30_000
MAX_HISTOGRAM_BUCKETS = len(HISTOGRAM_BOUNDS_MS) + 1
TRACE_ID_LIMIT = 256
DIMENSION_VALUE_LIMIT = 256
PACKAGE_VERSION_LIMIT = 64
HISTOGRAM_BUCKET_NAMES = (
    "bucket_0",
    "bucket_1",
    "bucket_2",
    "bucket_3",
    "bucket_4",
    "bucket_5",
    "bucket_6",
    "bucket_7",
    "bucket_8",
    "bucket_9",
    "bucket_10",
    "bucket_11",
    "bucket_12",
    "bucket_13",
)
HISTOGRAM_UPSERT_SQL = """
    INSERT INTO histograms(
        dimension, name, metric, count, total, maximum,
        bucket_0, bucket_1, bucket_2, bucket_3, bucket_4, bucket_5, bucket_6,
        bucket_7, bucket_8, bucket_9, bucket_10, bucket_11, bucket_12, bucket_13
    ) VALUES (
        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
    )
    ON CONFLICT(dimension, name, metric) DO UPDATE SET
        count = histograms.count + excluded.count,
        total = histograms.total + excluded.total,
        maximum = MAX(histograms.maximum, excluded.maximum),
        bucket_0 = histograms.bucket_0 + excluded.bucket_0,
        bucket_1 = histograms.bucket_1 + excluded.bucket_1,
        bucket_2 = histograms.bucket_2 + excluded.bucket_2,
        bucket_3 = histograms.bucket_3 + excluded.bucket_3,
        bucket_4 = histograms.bucket_4 + excluded.bucket_4,
        bucket_5 = histograms.bucket_5 + excluded.bucket_5,
        bucket_6 = histograms.bucket_6 + excluded.bucket_6,
        bucket_7 = histograms.bucket_7 + excluded.bucket_7,
        bucket_8 = histograms.bucket_8 + excluded.bucket_8,
        bucket_9 = histograms.bucket_9 + excluded.bucket_9,
        bucket_10 = histograms.bucket_10 + excluded.bucket_10,
        bucket_11 = histograms.bucket_11 + excluded.bucket_11,
        bucket_12 = histograms.bucket_12 + excluded.bucket_12,
        bucket_13 = histograms.bucket_13 + excluded.bucket_13
"""
HISTOGRAM_READ_SQL = """
    SELECT dimension, name, metric, count, total, maximum,
           bucket_0, bucket_1, bucket_2, bucket_3, bucket_4, bucket_5, bucket_6,
           bucket_7, bucket_8, bucket_9, bucket_10, bucket_11, bucket_12, bucket_13
    FROM histograms
    WHERE dimension != 'phase'
"""
PHASE_READ_SQL = """
    SELECT name, count, total, maximum,
           bucket_0, bucket_1, bucket_2, bucket_3, bucket_4, bucket_5, bucket_6,
           bucket_7, bucket_8, bucket_9, bucket_10, bucket_11, bucket_12, bucket_13
    FROM histograms
    WHERE dimension = 'phase' AND metric = 'duration_ms'
"""
DISTINCT_NAME_QUERIES = {
    "rollups": "SELECT DISTINCT name FROM rollups WHERE dimension = ?",
    "histograms": "SELECT DISTINCT name FROM histograms WHERE dimension = ?",
    "counters": "SELECT DISTINCT name FROM counters WHERE dimension = ?",
}

FailureOutcome = Literal[
    "success",
    "preflight_rejection",
    "result_refinement",
    "upstream_failure",
    "transport_failure",
    "cancellation",
    "internal_error",
]


@dataclass
class Histogram:
    bounds: tuple[int, ...] = HISTOGRAM_BOUNDS_MS
    counts: list[int] = field(default_factory=lambda: [0] * (len(HISTOGRAM_BOUNDS_MS) + 1))
    count: int = 0
    total: float = 0.0
    maximum: float = 0.0

    def observe(self, value: float) -> None:
        bounded = max(0.0, value)
        self.count += 1
        self.total += bounded
        self.maximum = max(self.maximum, bounded)
        for index, bound in enumerate(self.bounds):
            if bounded <= bound:
                self.counts[index] += 1
                return
        self.counts[-1] += 1

    def merge(self, other: Histogram) -> None:
        if self.bounds != other.bounds:
            raise ValueError("cannot merge histograms with different bounds")
        self.count += other.count
        self.total += other.total
        self.maximum = max(self.maximum, other.maximum)
        for index, value in enumerate(other.counts):
            self.counts[index] += value

    def snapshot(self) -> JsonObject:
        buckets: list[JsonObject] = [
            {"le": bound, "count": count}
            for bound, count in zip(self.bounds, self.counts[:-1], strict=True)
        ]
        buckets.append({"le": "inf", "count": self.counts[-1]})
        return {
            "count": self.count,
            "sum": round(self.total, 3),
            "average": round(self.total / self.count, 3) if self.count else 0.0,
            "max": round(self.maximum, 3),
            "buckets": JSON_VALUE_ADAPTER.validate_python(buckets),
        }


@dataclass
class Rollup:
    count: int = 0
    success: int = 0
    failure: int = 0
    input_bytes: int = 0
    output_bytes: int = 0
    calls: int = 0
    chain_calls: int = 0
    duration_ms: Histogram = field(default_factory=Histogram)
    input_size_bytes: Histogram = field(
        default_factory=lambda: Histogram(
            bounds=HISTOGRAM_BOUNDS_BYTES,
            counts=[0] * (len(HISTOGRAM_BOUNDS_BYTES) + 1),
        )
    )
    output_size_bytes: Histogram = field(
        default_factory=lambda: Histogram(
            bounds=HISTOGRAM_BOUNDS_BYTES,
            counts=[0] * (len(HISTOGRAM_BOUNDS_BYTES) + 1),
        )
    )

    def observe(
        self,
        *,
        duration_ms: float,
        success: bool,
        input_bytes: int = 0,
        output_bytes: int = 0,
        calls: int = 0,
        chain_calls: int = 0,
    ) -> None:
        self.count += 1
        if success:
            self.success += 1
        else:
            self.failure += 1
        self.input_bytes += max(0, input_bytes)
        self.output_bytes += max(0, output_bytes)
        self.calls += max(0, calls)
        self.chain_calls += max(0, chain_calls)
        self.duration_ms.observe(duration_ms)
        self.input_size_bytes.observe(float(max(0, input_bytes)))
        self.output_size_bytes.observe(float(max(0, output_bytes)))

    def merge(self, other: Rollup) -> None:
        self.count += other.count
        self.success += other.success
        self.failure += other.failure
        self.input_bytes += other.input_bytes
        self.output_bytes += other.output_bytes
        self.calls += other.calls
        self.chain_calls += other.chain_calls
        self.duration_ms.merge(other.duration_ms)
        self.input_size_bytes.merge(other.input_size_bytes)
        self.output_size_bytes.merge(other.output_size_bytes)

    def snapshot(self, *, include_distributions: bool = True) -> JsonObject:
        values: JsonObject = {
            "count": self.count,
            "success": self.success,
            "failure": self.failure,
            "input_bytes": self.input_bytes,
            "output_bytes": self.output_bytes,
            "calls": self.calls,
            "chain_calls": self.chain_calls,
        }
        if include_distributions:
            values["duration_ms"] = self.duration_ms.snapshot()
            values["input_size_bytes"] = self.input_size_bytes.snapshot()
            values["output_size_bytes"] = self.output_size_bytes.snapshot()
        return values


@dataclass(frozen=True)
class OperationFailure:
    stage: str
    trace_id: str
    subtype: str | None = None
    server: str | None = None
    tool: str | None = None


@dataclass(frozen=True)
class OperationObservation:
    duration_ms: float
    success: bool
    input_bytes: int = 0
    output_bytes: int = 0
    calls: int = 0
    chain_calls: int = 0
    failure: OperationFailure | None = None


@dataclass(frozen=True)
class FailureEvent:
    timestamp: int
    trace_id: str
    operation: str
    outcome: FailureOutcome
    stage: str
    subtype: str
    calls: int
    chain_calls: int
    server: str | None
    tool: str | None
    package_version: str

    def snapshot(self) -> JsonObject:
        values: JsonObject = {
            "timestamp": self.timestamp,
            "operation": self.operation,
            "outcome": self.outcome,
            "stage": self.stage,
            "subtype": self.subtype,
            "calls": self.calls,
            "chain_calls": self.chain_calls,
            "package_version": self.package_version,
        }
        values["trace_id"] = self.trace_id
        if self.server is not None:
            values["server"] = self.server
        if self.tool is not None:
            values["tool"] = self.tool
        return values


@dataclass
class StatsAccumulator:
    lifetime: Rollup = field(default_factory=Rollup)
    operations: dict[str, Rollup] = field(default_factory=dict)
    phases: dict[str, Histogram] = field(default_factory=dict)
    servers: dict[str, Rollup] = field(default_factory=dict)
    tools: dict[str, Rollup] = field(default_factory=dict)
    failures: dict[str, int] = field(default_factory=dict)
    outcomes: dict[str, int] = field(default_factory=dict)
    cache_hits: int = 0
    cache_misses: int = 0
    recent: dict[int, Rollup] = field(default_factory=dict)
    failure_events: list[FailureEvent] = field(default_factory=list)
    updated_at: int = 0

    def merge(self, other: StatsAccumulator) -> None:
        self.lifetime.merge(other.lifetime)
        _merge_rollup_mapping(self.operations, other.operations, MAX_OPERATIONS)
        _merge_histogram_mapping(self.phases, other.phases, MAX_PHASES)
        _merge_rollup_mapping(self.servers, other.servers, MAX_SERVERS)
        _merge_rollup_mapping(self.tools, other.tools, MAX_TOOLS)
        _merge_counter_mapping(self.failures, other.failures, MAX_FAILURE_STAGES)
        _merge_counter_mapping(self.outcomes, other.outcomes, MAX_OUTCOMES)
        self.cache_hits += other.cache_hits
        self.cache_misses += other.cache_misses
        for timestamp, rollup in other.recent.items():
            self.recent.setdefault(timestamp, Rollup()).merge(rollup)
        while len(self.recent) > RECENT_BUCKET_COUNT:
            del self.recent[min(self.recent)]
        self.failure_events.extend(other.failure_events)
        self.failure_events = self.failure_events[-RECENT_FAILURE_LIMIT:]
        self.updated_at = max(self.updated_at, other.updated_at)


class StatsStore:
    def __init__(self, path: Path, *, package_version: str = "unknown") -> None:
        self.path = path
        self.package_version = _bounded_text(package_version, PACKAGE_VERSION_LIMIT) or "unknown"
        self._delta = StatsAccumulator()
        self._dirty = False
        self._closing = False
        self._flush_task: asyncio.Task[None] | None = None
        self._flush_lock = asyncio.Lock()

    def record_operation(self, name: str, observation: OperationObservation) -> None:
        operation = _bounded_text(name, DIMENSION_VALUE_LIMIT) or OTHER_DIMENSION
        self._delta.lifetime.observe(
            duration_ms=observation.duration_ms,
            success=observation.success,
            input_bytes=observation.input_bytes,
            output_bytes=observation.output_bytes,
            calls=observation.calls,
            chain_calls=observation.chain_calls,
        )
        self._rollup_dimension(self._delta.operations, operation, MAX_OPERATIONS).observe(
            duration_ms=observation.duration_ms,
            success=observation.success,
            input_bytes=observation.input_bytes,
            output_bytes=observation.output_bytes,
            calls=observation.calls,
            chain_calls=observation.chain_calls,
        )
        self._recent_rollup().observe(
            duration_ms=observation.duration_ms,
            success=observation.success,
            input_bytes=observation.input_bytes,
            output_bytes=observation.output_bytes,
            calls=observation.calls,
            chain_calls=observation.chain_calls,
        )
        failure = observation.failure
        outcome = _operation_outcome(observation.success, failure)
        outcome_key = _bounded_key(self._delta.outcomes, outcome, MAX_OUTCOMES)
        self._delta.outcomes[outcome_key] = self._delta.outcomes.get(outcome_key, 0) + 1
        if failure is not None:
            stage = _bounded_text(failure.stage, DIMENSION_VALUE_LIMIT) or "unknown"
            key = _bounded_key(self._delta.failures, stage, MAX_FAILURE_STAGES)
            self._delta.failures[key] = self._delta.failures.get(key, 0) + 1
            subtype = _bounded_text(failure.subtype, DIMENSION_VALUE_LIMIT) or stage
            self._delta.failure_events.append(
                FailureEvent(
                    timestamp=int(time.time()),
                    trace_id=_required_bounded_text(failure.trace_id, TRACE_ID_LIMIT),
                    operation=operation,
                    outcome=outcome,
                    stage=stage,
                    subtype=subtype,
                    calls=max(0, observation.calls),
                    chain_calls=max(0, observation.chain_calls),
                    server=_bounded_text(failure.server, DIMENSION_VALUE_LIMIT),
                    tool=_bounded_text(failure.tool, DIMENSION_VALUE_LIMIT),
                    package_version=self.package_version,
                )
            )
            self._delta.failure_events = self._delta.failure_events[-RECENT_FAILURE_LIMIT:]
        self._changed()

    def record_phase(self, name: str, duration_ms: float) -> None:
        phase = _bounded_text(name, DIMENSION_VALUE_LIMIT) or OTHER_DIMENSION
        self._histogram_dimension(self._delta.phases, phase, MAX_PHASES).observe(duration_ms)
        self._changed()

    def record_upstream(
        self,
        server: str,
        tool: str,
        *,
        duration_ms: float,
        success: bool,
        input_bytes: int,
        output_bytes: int,
    ) -> None:
        server_name = _bounded_text(server, DIMENSION_VALUE_LIMIT) or OTHER_DIMENSION
        tool_name = _bounded_text(tool, DIMENSION_VALUE_LIMIT) or OTHER_DIMENSION
        for rollup in (
            self._rollup_dimension(self._delta.servers, server_name, MAX_SERVERS),
            self._rollup_dimension(
                self._delta.tools,
                f"{server_name}.{tool_name}",
                MAX_TOOLS,
            ),
        ):
            rollup.observe(
                duration_ms=duration_ms,
                success=success,
                input_bytes=input_bytes,
                output_bytes=output_bytes,
                calls=1,
            )
        self._changed()

    def record_cache(self, *, hit: bool) -> None:
        if hit:
            self._delta.cache_hits += 1
        else:
            self._delta.cache_misses += 1
        self._changed()

    async def snapshot(self) -> JsonObject:
        await self.flush()
        return await asyncio.to_thread(self._read_snapshot)

    def schedule_flush(self) -> None:
        if self._closing:
            return
        if self._flush_task is not None and not self._flush_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._flush_task = loop.create_task(self._delayed_flush())

    async def flush(self) -> None:
        async with self._flush_lock:
            if not self._dirty:
                return
            delta = self._delta
            self._delta = StatsAccumulator()
            self._dirty = False
            try:
                await asyncio.to_thread(self._merge_delta, delta)
            except BaseException:
                self._delta.merge(delta)
                self._dirty = True
                raise

    async def close(self) -> None:
        self._closing = True
        task, self._flush_task = self._flush_task, None
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await self.flush()

    async def _delayed_flush(self) -> None:
        try:
            await asyncio.sleep(FLUSH_DELAY_SECONDS)
            await self.flush()
        finally:
            self._flush_task = None
            if self._dirty and not self._closing:
                self.schedule_flush()

    def _changed(self) -> None:
        self._delta.updated_at = int(time.time())
        self._dirty = True
        self.schedule_flush()

    def _recent_rollup(self) -> Rollup:
        timestamp = int(time.time() // RECENT_BUCKET_SECONDS) * RECENT_BUCKET_SECONDS
        rollup = self._delta.recent.setdefault(timestamp, Rollup())
        while len(self._delta.recent) > RECENT_BUCKET_COUNT:
            del self._delta.recent[min(self._delta.recent)]
        return rollup

    @staticmethod
    def _rollup_dimension(values: dict[str, Rollup], key: str, limit: int) -> Rollup:
        bounded = _bounded_key(values, key, limit)
        return values.setdefault(bounded, Rollup())

    @staticmethod
    def _histogram_dimension(values: dict[str, Histogram], key: str, limit: int) -> Histogram:
        bounded = _bounded_key(values, key, limit)
        return values.setdefault(bounded, Histogram())

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.path,
            timeout=SQLITE_BUSY_TIMEOUT_MS / 1_000,
        )
        connection.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        _initialize_schema(connection)
        return connection

    def _merge_delta(self, delta: StatsAccumulator) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            _apply_delta(connection, delta)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _read_snapshot(self) -> JsonObject:
        connection = self._connect()
        try:
            rollups = _read_rollups(connection)
            phases = _read_phases(connection)
            counters = _read_counters(connection)
            lifetime = rollups.get("lifetime", {}).get("", Rollup())
            recent = [
                {
                    "bucket_start": int(timestamp),
                    **rollup.snapshot(include_distributions=False),
                }
                for timestamp, rollup in sorted(
                    rollups.get("recent", {}).items(),
                    key=lambda item: int(item[0]),
                )
            ]
            updated_row = connection.execute(
                "SELECT value FROM metadata WHERE key = 'updated_at'"
            ).fetchone()
            recent_failures = [
                FailureEvent(
                    timestamp=row[0],
                    trace_id=row[1],
                    operation=row[2],
                    outcome=row[3],
                    stage=row[4],
                    subtype=row[5],
                    calls=row[6],
                    chain_calls=row[7],
                    server=row[8],
                    tool=row[9],
                    package_version=row[10],
                ).snapshot()
                for row in connection.execute(
                    """
                    SELECT timestamp, trace_id, operation, outcome, stage, subtype,
                           calls, chain_calls, server, tool, package_version
                    FROM failure_events
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (RECENT_FAILURE_LIMIT,),
                )
            ]
            return JSON_OBJECT_ADAPTER.validate_python({
                "version": 2,
                "updated_at": updated_row[0] if updated_row is not None else 0,
                "lifetime": lifetime.snapshot(),
                "recent": recent,
                "operations": _snapshot_rollups(rollups.get("operation", {})),
                "phases": _snapshot_histograms(phases),
                "servers": _snapshot_rollups(rollups.get("server", {})),
                "tools": _snapshot_rollups(
                    rollups.get("tool", {}),
                    include_distributions=False,
                ),
                "failures": counters.get("failure", {}),
                "outcomes": counters.get("outcome", {}),
                "recent_failures": recent_failures,
                "cache": {
                    "hits": counters.get("cache", {}).get("hits", 0),
                    "misses": counters.get("cache", {}).get("misses", 0),
                },
            })
        finally:
            connection.close()


def _apply_delta(connection: sqlite3.Connection, delta: StatsAccumulator) -> None:
    _merge_rollups(connection, "lifetime", {"": delta.lifetime}, 1)
    _merge_rollups(connection, "operation", delta.operations, MAX_OPERATIONS)
    _merge_histograms(connection, "phase", delta.phases, MAX_PHASES, "duration_ms")
    _merge_rollups(connection, "server", delta.servers, MAX_SERVERS)
    _merge_rollups(connection, "tool", delta.tools, MAX_TOOLS)
    _merge_rollups(
        connection,
        "recent",
        {str(timestamp): rollup for timestamp, rollup in delta.recent.items()},
        RECENT_BUCKET_COUNT,
        bound_names=False,
        include_histograms=False,
    )
    _merge_counters(connection, "failure", delta.failures, MAX_FAILURE_STAGES)
    _merge_counters(connection, "outcome", delta.outcomes, MAX_OUTCOMES)
    _merge_counters(
        connection,
        "cache",
        {"hits": delta.cache_hits, "misses": delta.cache_misses},
        3,
    )
    connection.execute(
        """
        INSERT INTO metadata(key, value) VALUES ('updated_at', ?)
        ON CONFLICT(key) DO UPDATE SET value = MAX(metadata.value, excluded.value)
        """,
        (delta.updated_at,),
    )
    for event in delta.failure_events:
        connection.execute(
            """
            INSERT INTO failure_events(
                timestamp, trace_id, operation, outcome, stage, subtype,
                calls, chain_calls, server, tool, package_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.timestamp,
                event.trace_id,
                event.operation,
                event.outcome,
                event.stage,
                event.subtype,
                event.calls,
                event.chain_calls,
                event.server,
                event.tool,
                event.package_version,
            ),
        )
    connection.execute(
        """
        DELETE FROM failure_events
        WHERE id NOT IN (
            SELECT id FROM failure_events ORDER BY id DESC LIMIT ?
        )
        """,
        (RECENT_FAILURE_LIMIT,),
    )
    connection.execute(
        """
        DELETE FROM rollups
        WHERE dimension = 'recent'
          AND name NOT IN (
            SELECT name FROM rollups
            WHERE dimension = 'recent'
            ORDER BY CAST(name AS INTEGER) DESC
            LIMIT ?
          )
        """,
        (RECENT_BUCKET_COUNT,),
    )


def _initialize_schema(connection: sqlite3.Connection) -> None:
    bucket_columns = ",\n".join(
        f"bucket_{index} INTEGER NOT NULL DEFAULT 0" for index in range(MAX_HISTOGRAM_BUCKETS)
    )
    connection.executescript(f"""
        CREATE TABLE IF NOT EXISTS metadata(
            key TEXT PRIMARY KEY,
            value INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS rollups(
            dimension TEXT NOT NULL,
            name TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            success INTEGER NOT NULL DEFAULT 0,
            failure INTEGER NOT NULL DEFAULT 0,
            input_bytes INTEGER NOT NULL DEFAULT 0,
            output_bytes INTEGER NOT NULL DEFAULT 0,
            calls INTEGER NOT NULL DEFAULT 0,
            chain_calls INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(dimension, name)
        );
        CREATE TABLE IF NOT EXISTS histograms(
            dimension TEXT NOT NULL,
            name TEXT NOT NULL,
            metric TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            total REAL NOT NULL DEFAULT 0,
            maximum REAL NOT NULL DEFAULT 0,
            {bucket_columns},
            PRIMARY KEY(dimension, name, metric)
        );
        CREATE TABLE IF NOT EXISTS counters(
            dimension TEXT NOT NULL,
            name TEXT NOT NULL,
            value INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(dimension, name)
        );
        CREATE TABLE IF NOT EXISTS failure_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER NOT NULL,
            trace_id TEXT NOT NULL,
            operation TEXT NOT NULL,
            outcome TEXT NOT NULL,
            stage TEXT NOT NULL,
            subtype TEXT NOT NULL,
            calls INTEGER NOT NULL,
            chain_calls INTEGER NOT NULL,
            server TEXT,
            tool TEXT,
            package_version TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS failure_events_timestamp
            ON failure_events(timestamp DESC);
        PRAGMA user_version = {SQLITE_SCHEMA_VERSION};
    """)


def _merge_rollups(
    connection: sqlite3.Connection,
    dimension: str,
    values: Mapping[str, Rollup],
    limit: int,
    *,
    bound_names: bool = True,
    include_histograms: bool = True,
) -> None:
    name_mapping = (
        _bounded_database_names(connection, "rollups", dimension, values, limit)
        if bound_names
        else {name: name for name in values}
    )
    merged: dict[str, Rollup] = {}
    for original_name, rollup in values.items():
        merged.setdefault(name_mapping[original_name], Rollup()).merge(rollup)
    for name, rollup in merged.items():
        connection.execute(
            """
            INSERT INTO rollups(
                dimension, name, count, success, failure, input_bytes,
                output_bytes, calls, chain_calls
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(dimension, name) DO UPDATE SET
                count = rollups.count + excluded.count,
                success = rollups.success + excluded.success,
                failure = rollups.failure + excluded.failure,
                input_bytes = rollups.input_bytes + excluded.input_bytes,
                output_bytes = rollups.output_bytes + excluded.output_bytes,
                calls = rollups.calls + excluded.calls,
                chain_calls = rollups.chain_calls + excluded.chain_calls
            """,
            (
                dimension,
                name,
                rollup.count,
                rollup.success,
                rollup.failure,
                rollup.input_bytes,
                rollup.output_bytes,
                rollup.calls,
                rollup.chain_calls,
            ),
        )
        if include_histograms:
            _upsert_histogram(connection, dimension, name, "duration_ms", rollup.duration_ms)
            _upsert_histogram(
                connection,
                dimension,
                name,
                "input_size_bytes",
                rollup.input_size_bytes,
            )
            _upsert_histogram(
                connection,
                dimension,
                name,
                "output_size_bytes",
                rollup.output_size_bytes,
            )


def _merge_histograms(
    connection: sqlite3.Connection,
    dimension: str,
    values: Mapping[str, Histogram],
    limit: int,
    metric: str,
) -> None:
    name_mapping = _bounded_database_names(connection, "histograms", dimension, values, limit)
    merged: dict[str, Histogram] = {}
    for original_name, histogram in values.items():
        target = merged.setdefault(
            name_mapping[original_name],
            Histogram(bounds=histogram.bounds, counts=[0] * len(histogram.counts)),
        )
        target.merge(histogram)
    for name, histogram in merged.items():
        _upsert_histogram(connection, dimension, name, metric, histogram)


def _upsert_histogram(
    connection: sqlite3.Connection,
    dimension: str,
    name: str,
    metric: str,
    histogram: Histogram,
) -> None:
    bucket_values = [
        *histogram.counts,
        *([0] * (MAX_HISTOGRAM_BUCKETS - len(histogram.counts))),
    ]
    connection.execute(
        HISTOGRAM_UPSERT_SQL,
        (
            dimension,
            name,
            metric,
            histogram.count,
            histogram.total,
            histogram.maximum,
            *bucket_values,
        ),
    )


def _merge_counters(
    connection: sqlite3.Connection,
    dimension: str,
    values: Mapping[str, int],
    limit: int,
) -> None:
    name_mapping = _bounded_database_names(connection, "counters", dimension, values, limit)
    merged: dict[str, int] = {}
    for original_name, value in values.items():
        target = name_mapping[original_name]
        merged[target] = merged.get(target, 0) + value
    for name, value in merged.items():
        connection.execute(
            """
            INSERT INTO counters(dimension, name, value) VALUES (?, ?, ?)
            ON CONFLICT(dimension, name) DO UPDATE SET
                value = counters.value + excluded.value
            """,
            (dimension, name, value),
        )


def _bounded_database_names(
    connection: sqlite3.Connection,
    table: Literal["rollups", "histograms", "counters"],
    dimension: str,
    values: Mapping[str, object],
    limit: int,
) -> dict[str, str]:
    existing = {
        str(row[0])
        for row in connection.execute(
            DISTINCT_NAME_QUERIES[table],
            (dimension,),
        )
    }
    mapping: dict[str, str] = {}
    for name in sorted(values):
        if name in existing:
            mapping[name] = name
        elif len(existing - {OTHER_DIMENSION}) < max(1, limit - 1):
            existing.add(name)
            mapping[name] = name
        else:
            existing.add(OTHER_DIMENSION)
            mapping[name] = OTHER_DIMENSION
    return mapping


def _read_rollups(connection: sqlite3.Connection) -> dict[str, dict[str, Rollup]]:
    histograms = _read_rollup_histograms(connection)
    result: dict[str, dict[str, Rollup]] = {}
    for row in connection.execute(
        """
        SELECT dimension, name, count, success, failure, input_bytes,
               output_bytes, calls, chain_calls
        FROM rollups
        """
    ):
        dimension, name = str(row[0]), str(row[1])
        rollup = Rollup(
            count=row[2],
            success=row[3],
            failure=row[4],
            input_bytes=row[5],
            output_bytes=row[6],
            calls=row[7],
            chain_calls=row[8],
        )
        rollup.duration_ms = histograms.get(
            (dimension, name, "duration_ms"),
            Histogram(),
        )
        rollup.input_size_bytes = histograms.get(
            (dimension, name, "input_size_bytes"),
            Histogram(
                bounds=HISTOGRAM_BOUNDS_BYTES,
                counts=[0] * (len(HISTOGRAM_BOUNDS_BYTES) + 1),
            ),
        )
        rollup.output_size_bytes = histograms.get(
            (dimension, name, "output_size_bytes"),
            Histogram(
                bounds=HISTOGRAM_BOUNDS_BYTES,
                counts=[0] * (len(HISTOGRAM_BOUNDS_BYTES) + 1),
            ),
        )
        result.setdefault(dimension, {})[name] = rollup
    return result


def _read_rollup_histograms(
    connection: sqlite3.Connection,
) -> dict[tuple[str, str, str], Histogram]:
    result: dict[tuple[str, str, str], Histogram] = {}
    for row in connection.execute(HISTOGRAM_READ_SQL):
        dimension, name, metric = str(row[0]), str(row[1]), str(row[2])
        bounds = HISTOGRAM_BOUNDS_BYTES if metric.endswith("size_bytes") else HISTOGRAM_BOUNDS_MS
        result[dimension, name, metric] = Histogram(
            bounds=bounds,
            counts=list(row[6 : 6 + len(bounds) + 1]),
            count=row[3],
            total=row[4],
            maximum=row[5],
        )
    return result


def _read_phases(connection: sqlite3.Connection) -> dict[str, Histogram]:
    return {
        str(row[0]): Histogram(
            counts=list(row[4 : 4 + len(HISTOGRAM_BOUNDS_MS) + 1]),
            count=row[1],
            total=row[2],
            maximum=row[3],
        )
        for row in connection.execute(PHASE_READ_SQL)
    }


def _read_counters(connection: sqlite3.Connection) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for dimension, name, value in connection.execute("SELECT dimension, name, value FROM counters"):
        result.setdefault(str(dimension), {})[str(name)] = int(value)
    return result


def _snapshot_rollups(
    values: Mapping[str, Rollup],
    *,
    include_distributions: bool = True,
) -> JsonObject:
    return {
        key: value.snapshot(include_distributions=include_distributions)
        for key, value in sorted(values.items())
    }


def _snapshot_histograms(values: Mapping[str, Histogram]) -> JsonObject:
    return {key: value.snapshot() for key, value in sorted(values.items())}


def _merge_rollup_mapping(
    target: dict[str, Rollup],
    source: Mapping[str, Rollup],
    limit: int,
) -> None:
    for key, value in source.items():
        bounded = _bounded_key(target, key, limit)
        target.setdefault(bounded, Rollup()).merge(value)


def _merge_histogram_mapping(
    target: dict[str, Histogram],
    source: Mapping[str, Histogram],
    limit: int,
) -> None:
    for key, value in source.items():
        bounded = _bounded_key(target, key, limit)
        histogram = target.setdefault(
            bounded,
            Histogram(bounds=value.bounds, counts=[0] * len(value.counts)),
        )
        histogram.merge(value)


def _merge_counter_mapping(
    target: dict[str, int],
    source: Mapping[str, int],
    limit: int,
) -> None:
    for key, value in source.items():
        bounded = _bounded_key(target, key, limit)
        target[bounded] = target.get(bounded, 0) + value


def _operation_outcome(
    success: bool,
    failure: OperationFailure | None,
) -> FailureOutcome:
    if success:
        return "success"
    if failure is None:
        return "internal_error"
    if failure.stage == "preflight":
        return "preflight_rejection"
    if failure.stage == "result":
        return "result_refinement"
    if failure.stage == "cancelled":
        return "cancellation"
    if failure.subtype == "upstream_transport" or failure.stage == "timeout":
        return "transport_failure"
    if failure.stage in {"runtime", "discovery"}:
        return "upstream_failure"
    return "internal_error"


def _bounded_key(values: Mapping[str, object], key: str, limit: int) -> str:
    if key in values:
        return key
    if len(values) < max(1, limit - 1):
        return key
    return OTHER_DIMENSION


def _required_bounded_text(value: str, limit: int) -> str:
    bounded = _bounded_text(value, limit)
    if bounded is None:
        raise ValueError("required telemetry text must not be empty")
    return bounded


def _bounded_text(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    compact = value.strip()
    if not compact:
        return None
    return compact[:limit]
