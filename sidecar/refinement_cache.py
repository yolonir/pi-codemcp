from __future__ import annotations

import math
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from .json_types import JSON_VALUE_ADAPTER, JsonValue

if TYPE_CHECKING:
    from collections.abc import Callable

DEFAULT_REFINEMENT_ENTRY_LIMIT = 8
DEFAULT_REFINEMENT_ENTRY_BYTES = 1024 * 1024
DEFAULT_REFINEMENT_TOTAL_BYTES = 4 * 1024 * 1024
DEFAULT_REFINEMENT_TTL_SECONDS = 300.0


type ReferenceFailureReason = Literal[
    "expired",
    "evicted",
    "cross_sidecar",
    "unknown",
]


@dataclass(frozen=True, slots=True)
class RetainedResult:
    reference: str
    expires_in_seconds: int


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    payload: bytes
    expires_at: float


class ResultReferenceError(ValueError):
    def __init__(self, reason: ReferenceFailureReason, reference: str) -> None:
        self.reason = reason
        self.reference = reference
        messages = {
            "expired": "Result reference has expired",
            "evicted": "Result reference was evicted",
            "cross_sidecar": "Result reference belongs to another sidecar",
            "unknown": "Unknown result reference",
        }
        super().__init__(f"{messages[reason]}: {reference}")


class RefinementCache:
    def __init__(
        self,
        *,
        entry_limit: int = DEFAULT_REFINEMENT_ENTRY_LIMIT,
        entry_byte_limit: int = DEFAULT_REFINEMENT_ENTRY_BYTES,
        total_byte_limit: int = DEFAULT_REFINEMENT_TOTAL_BYTES,
        ttl_seconds: float = DEFAULT_REFINEMENT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if entry_limit < 1:
            raise ValueError("entry_limit must be positive")
        if entry_byte_limit < 1:
            raise ValueError("entry_byte_limit must be positive")
        if total_byte_limit < entry_byte_limit:
            raise ValueError("total_byte_limit must be at least entry_byte_limit")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self.entry_limit = entry_limit
        self.entry_byte_limit = entry_byte_limit
        self.total_byte_limit = total_byte_limit
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._instance_id = uuid.uuid4().hex[:12]
        self._prefix = f"result_{self._instance_id}_"
        self._entries: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._invalid: OrderedDict[str, ReferenceFailureReason] = OrderedDict()
        self._total_bytes = 0

    @property
    def entry_count(self) -> int:
        return len(self._entries)

    @property
    def total_bytes(self) -> int:
        return self._total_bytes

    def retain(self, value: JsonValue) -> RetainedResult | None:
        payload = JSON_VALUE_ADAPTER.dump_json(value)
        payload_bytes = len(payload)
        if payload_bytes > self.entry_byte_limit or payload_bytes > self.total_byte_limit:
            return None
        now = self._clock()
        self._prune_expired(now)
        while (
            len(self._entries) >= self.entry_limit
            or self._total_bytes + payload_bytes > self.total_byte_limit
        ):
            self._evict_oldest()
        reference = f"{self._prefix}{uuid.uuid4().hex[:16]}"
        self._entries[reference] = _CacheEntry(
            payload=payload,
            expires_at=now + self.ttl_seconds,
        )
        self._total_bytes += payload_bytes
        return RetainedResult(
            reference=reference,
            expires_in_seconds=math.ceil(self.ttl_seconds),
        )

    def resolve(self, reference: str) -> JsonValue:
        now = self._clock()
        self._prune_expired(now)
        entry = self._entries.get(reference)
        if entry is not None:
            self._entries.move_to_end(reference)
            return JSON_VALUE_ADAPTER.validate_json(entry.payload)
        reason = self._invalid.get(reference)
        if reason is not None:
            raise ResultReferenceError(reason, reference)
        if not reference.startswith(self._prefix):
            raise ResultReferenceError("cross_sidecar", reference)
        raise ResultReferenceError("unknown", reference)

    def clear(self) -> None:
        self._entries.clear()
        self._invalid.clear()
        self._total_bytes = 0

    def _prune_expired(self, now: float) -> None:
        expired = [
            reference for reference, entry in self._entries.items() if entry.expires_at <= now
        ]
        for reference in expired:
            self._remove(reference, "expired")

    def _evict_oldest(self) -> None:
        reference = next(iter(self._entries))
        self._remove(reference, "evicted")

    def _remove(
        self,
        reference: str,
        reason: Literal["expired", "evicted"],
    ) -> None:
        entry = self._entries.pop(reference)
        self._total_bytes -= len(entry.payload)
        self._invalid[reference] = reason
        tombstone_limit = self.entry_limit * 4
        while len(self._invalid) > tombstone_limit:
            self._invalid.popitem(last=False)
