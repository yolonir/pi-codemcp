from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .json_types import JSON_OBJECT_ADAPTER, JSON_VALUE_ADAPTER, JsonObject

if TYPE_CHECKING:
    from collections.abc import Mapping

HISTOGRAM_BOUNDS_MS = (1, 5, 10, 25, 50, 100, 250, 500, 1_000, 2_500, 5_000, 10_000, 30_000)
HISTOGRAM_BOUNDS_BYTES = (64, 256, 1_024, 4_096, 16_384, 65_536, 262_144, 1_048_576)
RECENT_BUCKET_SECONDS = 60 * 60
RECENT_BUCKET_COUNT = 24
FLUSH_DELAY_SECONDS = 5.0
MAX_OPERATIONS = 32
MAX_PHASES = 16
MAX_SERVERS = 64
MAX_TOOLS = 384
MAX_FAILURE_STAGES = 16
OTHER_DIMENSION = "<other>"


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

    @classmethod
    def from_snapshot(
        cls,
        value: object,
        *,
        bounds: tuple[int, ...] = HISTOGRAM_BOUNDS_MS,
    ) -> Histogram:
        histogram = cls(bounds=bounds, counts=[0] * (len(bounds) + 1))
        if not isinstance(value, dict):
            return histogram
        histogram.count = _integer(value.get("count"))
        histogram.total = _number(value.get("sum"))
        histogram.maximum = _number(value.get("max"))
        raw_buckets = value.get("buckets")
        if isinstance(raw_buckets, list) and len(raw_buckets) == len(histogram.counts):
            histogram.counts = [
                _integer(bucket.get("count")) if isinstance(bucket, dict) else 0
                for bucket in raw_buckets
            ]
        return histogram


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

    @classmethod
    def from_snapshot(cls, value: object) -> Rollup:
        if not isinstance(value, dict):
            return cls()
        return cls(
            count=_integer(value.get("count")),
            success=_integer(value.get("success")),
            failure=_integer(value.get("failure")),
            input_bytes=_integer(value.get("input_bytes")),
            output_bytes=_integer(value.get("output_bytes")),
            calls=_integer(value.get("calls")),
            chain_calls=_integer(value.get("chain_calls")),
            duration_ms=Histogram.from_snapshot(value.get("duration_ms")),
            input_size_bytes=Histogram.from_snapshot(
                value.get("input_size_bytes"), bounds=HISTOGRAM_BOUNDS_BYTES
            ),
            output_size_bytes=Histogram.from_snapshot(
                value.get("output_size_bytes"), bounds=HISTOGRAM_BOUNDS_BYTES
            ),
        )


class StatsStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lifetime = Rollup()
        self.operations: dict[str, Rollup] = {}
        self.phases: dict[str, Histogram] = {}
        self.servers: dict[str, Rollup] = {}
        self.tools: dict[str, Rollup] = {}
        self.failures: dict[str, int] = {}
        self.cache_hits = 0
        self.cache_misses = 0
        self.recent: dict[int, Rollup] = {}
        self.updated_at = 0
        self._dirty = False
        self._closing = False
        self._flush_task: asyncio.Task[None] | None = None
        self._load()

    def record_operation(
        self,
        name: str,
        *,
        duration_ms: float,
        success: bool,
        failure_stage: str | None = None,
        input_bytes: int = 0,
        output_bytes: int = 0,
        calls: int = 0,
        chain_calls: int = 0,
    ) -> None:
        self.lifetime.observe(
            duration_ms=duration_ms,
            success=success,
            input_bytes=input_bytes,
            output_bytes=output_bytes,
            calls=calls,
            chain_calls=chain_calls,
        )
        self._rollup_dimension(self.operations, name, MAX_OPERATIONS).observe(
            duration_ms=duration_ms,
            success=success,
            input_bytes=input_bytes,
            output_bytes=output_bytes,
            calls=calls,
            chain_calls=chain_calls,
        )
        self._recent_rollup().observe(
            duration_ms=duration_ms,
            success=success,
            input_bytes=input_bytes,
            output_bytes=output_bytes,
            calls=calls,
            chain_calls=chain_calls,
        )
        if failure_stage is not None:
            key = self._bounded_key(self.failures, failure_stage, MAX_FAILURE_STAGES)
            self.failures[key] = self.failures.get(key, 0) + 1
        self._changed()

    def record_phase(self, name: str, duration_ms: float) -> None:
        self._histogram_dimension(self.phases, name, MAX_PHASES).observe(duration_ms)
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
        for rollup in (
            self._rollup_dimension(self.servers, server, MAX_SERVERS),
            self._rollup_dimension(self.tools, f"{server}.{tool}", MAX_TOOLS),
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
            self.cache_hits += 1
        else:
            self.cache_misses += 1
        self._changed()

    def snapshot(self) -> JsonObject:
        recent = [
            {"bucket_start": timestamp, **rollup.snapshot(include_distributions=False)}
            for timestamp, rollup in sorted(self.recent.items())
        ]
        return JSON_OBJECT_ADAPTER.validate_python({
            "version": 1,
            "updated_at": self.updated_at,
            "lifetime": self.lifetime.snapshot(),
            "recent": recent,
            "operations": _snapshot_mapping(self.operations),
            "phases": _snapshot_mapping(self.phases),
            "servers": _snapshot_mapping(self.servers),
            "tools": _snapshot_mapping(self.tools, include_distributions=False),
            "failures": dict(sorted(self.failures.items())),
            "cache": {"hits": self.cache_hits, "misses": self.cache_misses},
        })

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
        if not self._dirty:
            return
        payload = self.snapshot()
        self._dirty = False
        try:
            await asyncio.to_thread(self._write, payload)
        except BaseException:
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
        self.updated_at = int(time.time())
        self._dirty = True
        self.schedule_flush()

    def _recent_rollup(self) -> Rollup:
        timestamp = int(time.time() // RECENT_BUCKET_SECONDS) * RECENT_BUCKET_SECONDS
        rollup = self.recent.setdefault(timestamp, Rollup())
        while len(self.recent) > RECENT_BUCKET_COUNT:
            del self.recent[min(self.recent)]
        return rollup

    @staticmethod
    def _rollup_dimension(values: dict[str, Rollup], key: str, limit: int) -> Rollup:
        bounded = StatsStore._bounded_key(values, key, limit)
        return values.setdefault(bounded, Rollup())

    @staticmethod
    def _histogram_dimension(values: dict[str, Histogram], key: str, limit: int) -> Histogram:
        bounded = StatsStore._bounded_key(values, key, limit)
        return values.setdefault(bounded, Histogram())

    @staticmethod
    def _bounded_key(values: Mapping[str, object], key: str, limit: int) -> str:
        if key in values:
            return key
        if len(values) < max(1, limit - 1):
            return key
        return OTHER_DIMENSION

    def _write(self, payload: JsonObject) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(payload, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )
        Path(temporary).replace(self.path)

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        if not isinstance(raw, dict) or raw.get("version") != 1:
            return
        self.updated_at = _integer(raw.get("updated_at"))
        self.lifetime = Rollup.from_snapshot(raw.get("lifetime"))
        self.operations = _load_rollups(raw.get("operations"), MAX_OPERATIONS)
        self.phases = _load_histograms(raw.get("phases"), MAX_PHASES)
        self.servers = _load_rollups(raw.get("servers"), MAX_SERVERS)
        self.tools = _load_rollups(raw.get("tools"), MAX_TOOLS)
        raw_failures = raw.get("failures")
        if isinstance(raw_failures, dict):
            self.failures = {
                str(key): _integer(value)
                for key, value in list(raw_failures.items())[:MAX_FAILURE_STAGES]
            }
        raw_cache = raw.get("cache")
        if isinstance(raw_cache, dict):
            self.cache_hits = _integer(raw_cache.get("hits"))
            self.cache_misses = _integer(raw_cache.get("misses"))
        raw_recent = raw.get("recent")
        if isinstance(raw_recent, list):
            for item in raw_recent[-RECENT_BUCKET_COUNT:]:
                if not isinstance(item, dict):
                    continue
                timestamp = _integer(item.get("bucket_start"))
                if timestamp > 0:
                    self.recent[timestamp] = Rollup.from_snapshot(item)


def _snapshot_mapping(
    values: Mapping[str, Rollup | Histogram],
    *,
    include_distributions: bool = True,
) -> JsonObject:
    return {
        key: (
            value.snapshot(include_distributions=include_distributions)
            if isinstance(value, Rollup)
            else value.snapshot()
        )
        for key, value in sorted(values.items())
    }


def _load_rollups(value: object, limit: int) -> dict[str, Rollup]:
    if not isinstance(value, dict):
        return {}
    return {str(key): Rollup.from_snapshot(item) for key, item in list(value.items())[:limit]}


def _load_histograms(value: object, limit: int) -> dict[str, Histogram]:
    if not isinstance(value, dict):
        return {}
    return {str(key): Histogram.from_snapshot(item) for key, item in list(value.items())[:limit]}


def _integer(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _number(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0
