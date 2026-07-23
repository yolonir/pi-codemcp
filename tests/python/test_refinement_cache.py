from __future__ import annotations

from dataclasses import dataclass

import pytest

from sidecar.refinement_cache import (
    RefinementCache,
    ResultReferenceError,
)


@dataclass
class Clock:
    now: float = 0.0

    def __call__(self) -> float:
        return self.now


def test_refinement_cache_resolves_and_rejects_cross_sidecar_references() -> None:
    cache = RefinementCache(entry_byte_limit=100, total_byte_limit=200)
    retained = cache.retain({"items": [1, 2, 3]})
    assert retained is not None
    assert retained.reference.startswith("result_")
    assert retained.expires_in_seconds == 300
    assert cache.resolve(retained.reference) == {"items": [1, 2, 3]}

    other = RefinementCache(entry_byte_limit=100, total_byte_limit=200)
    with pytest.raises(ResultReferenceError, match="another sidecar") as cross_sidecar:
        other.resolve(retained.reference)
    assert cross_sidecar.value.reason == "cross_sidecar"


def test_refinement_cache_reports_expiry_eviction_unknown_and_clear() -> None:
    clock = Clock()
    cache = RefinementCache(
        entry_limit=2,
        entry_byte_limit=100,
        total_byte_limit=200,
        ttl_seconds=5,
        clock=clock,
    )
    first = cache.retain({"value": "first"})
    second = cache.retain({"value": "second"})
    third = cache.retain({"value": "third"})
    assert first is not None and second is not None and third is not None
    assert cache.entry_count == 2
    with pytest.raises(ResultReferenceError, match="evicted") as evicted:
        cache.resolve(first.reference)
    assert evicted.value.reason == "evicted"

    clock.now = 6
    with pytest.raises(ResultReferenceError, match="expired") as expired:
        cache.resolve(second.reference)
    assert expired.value.reason == "expired"

    unknown = f"{third.reference}_unknown"
    with pytest.raises(ResultReferenceError, match="Unknown") as missing:
        cache.resolve(unknown)
    assert missing.value.reason == "unknown"

    replacement = cache.retain({"value": "replacement"})
    assert replacement is not None
    cache.clear()
    assert cache.entry_count == 0
    assert cache.total_bytes == 0
    with pytest.raises(ResultReferenceError, match="Unknown"):
        cache.resolve(replacement.reference)


def test_refinement_cache_enforces_entry_and_total_byte_limits() -> None:
    cache = RefinementCache(
        entry_limit=4,
        entry_byte_limit=40,
        total_byte_limit=60,
    )
    first = cache.retain("a" * 25)
    second = cache.retain("b" * 25)
    assert first is not None and second is not None
    assert cache.entry_count == 2

    third = cache.retain("c" * 25)
    assert third is not None
    assert cache.entry_count == 2
    with pytest.raises(ResultReferenceError, match="evicted"):
        cache.resolve(first.reference)
    assert cache.total_bytes <= cache.total_byte_limit

    assert cache.retain("x" * 100) is None
