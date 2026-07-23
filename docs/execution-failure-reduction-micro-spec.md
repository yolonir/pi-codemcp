# Execution Failure Reduction Micro-Spec

**Status:** implemented
**Date:** 2026-07-23
**Scope:** trustworthy telemetry, typed-sandbox ergonomics, connection recovery, and oversized-result refinement

## Goal

Reduce repeated `codemcp_execute` failures without weakening type safety, hiding upstream errors, increasing model-context volume, or blindly retrying calls that may have side effects.

## Evidence baseline

A read-only review of Pi sessions covered the retained telemetry window from `2026-07-18T23:00:00Z` through `2026-07-23T08:40:54Z`.

After fork deduplication, the archive contained:

- 311 unique `codemcp_execute` results;
- 255 successful executions;
- 55 structured execution failures;
- one transport abort;
- 56 unique failed attempts across 23 user tasks;
- one deliberate negative test, leaving 55 organic failed attempts.

Observed failure classes:

| Class | Attempts |
|---|---:|
| Typed-sandbox misuse or friction | 26 |
| Oversized final result | 10 |
| Invalid upstream query or request | 10 |
| Access, resource state, or transport | 9 |
| Cancellation | 1 |

The first subsequent CodeMCP execution in the same branch succeeded after 40 failures, failed again after 12, and was not observed after four. Three broad investigation tasks accounted for 22 of the 56 failures.

The existing telemetry file is not a trustworthy cumulative source. Two snapshots eight minutes apart changed from 194 lifetime runs to 193 while new executions were recorded. Each Pi session owns a sidecar process, each sidecar loads the shared snapshot once, and each `StatsStore` later replaces the complete file. Concurrent writers therefore overwrite one another.

The current rollup also intentionally contains no event or session references, so a failure count cannot be joined back to the Pi session that produced it.

## Decisions

### 1. Make telemetry process-safe and traceable

Replace the shared JSON snapshot with one process-safe SQLite telemetry store. SQLite becomes the sole telemetry source of truth; there is no dual-write compatibility path, and the unreliable historical `stats.json` is not imported.

Each sidecar keeps bounded in-memory deltas and merges them transactionally into SQLite on flush. Rollups and histogram buckets use additive upserts so concurrent sidecars cannot decrease or overwrite counters.

Pass the Pi tool call ID, currently ignored by the TypeScript tool implementation, to the sidecar as an opaque `trace_id`.

Keep a bounded ring of the latest 200 failed operations containing only:

- timestamp;
- opaque trace ID;
- operation;
- failure stage and stable subtype;
- completed MCP and nested-chain call counts;
- upstream server and tool when known;
- package version.

Do not store model code, arguments, results, queries, error text, credentials, or other payload data.

Report telemetry outcomes separately:

- successful execution;
- preflight rejection;
- result requiring refinement;
- upstream failure;
- transport failure;
- cancellation;
- internal error.

A preflight rejection remains visible but is not presented as equivalent to an operational runtime failure.

### 2. Move common sandbox mistakes into preflight

Publish the exact `inspect_json` limits in its generated stub:

```python
def inspect_json(
    value: JsonValue,
    *,
    samples: Literal[1, 2, 3] = 2,
    max_depth: Literal[1, 2, 3, 4, 5, 6] = 3,
) -> JsonValue: ...
```

This makes invalid literal bounds fail type checking before any upstream call executes.

Add prebound narrowing helpers with runtime validation and precise return types:

```python
def expect_object(value: JsonValue) -> dict[str, JsonValue]: ...
def expect_list(value: JsonValue) -> list[JsonValue]: ...
def expect_string(value: JsonValue) -> str: ...
def expect_integer(value: JsonValue) -> int: ...
```

Expose these helpers in the shared prelude returned by search and inspect. They complement, rather than replace, upstream output schemas. Values remain `JsonValue` when an upstream server does not publish a usable output schema.

Make the sandbox capability prelude exact and concise. It must identify the supported import and `asyncio` surface and explicitly reject unavailable operations observed in sessions, including `collections.Counter`, `base64`, `gzip`, `asyncio.create_task`, and `__import__`. Where a concise safe equivalent exists, name it.

Do not solve typing friction by accepting `Any`, casting away errors, or weakening generated argument types.

### 3. Recover connections without replaying calls

When an upstream call fails because its connection is closed or otherwise unusable:

1. record the original call as failed;
2. invalidate and close the cached upstream client;
3. return a structured transport failure marked `retryable: true`;
4. let the next explicit call establish a fresh connection.

Do not automatically replay the failed tool call. It may have completed upstream or may carry side effects.

Return stable machine-readable failure fields in addition to the concise message:

```json
{
  "kind": "upstream_transport",
  "server": "grafana",
  "tool": "query_prometheus",
  "retryable": true,
  "status": null,
  "message": "Connection closed"
}
```

Automatic retry, if ever added, requires a separate decision and must be limited to tools whose authoritative MCP annotations establish that replay is safe.

### 4. Refine oversized values without repeating upstream calls

An oversized final result remains an explicit `result` failure and is never silently truncated or semantically compacted.

When the value fits within a separate bounded refinement cache, retain it temporarily in sidecar memory and return an opaque reference alongside the existing shape diagnostics:

```json
{
  "ok": false,
  "failure_stage": "result",
  "error": "Returned value is 90792 bytes; reduce it below 65536 bytes",
  "result_ref": "result_abc123",
  "shape": {},
  "calls_made": 3,
  "expires_in_seconds": 300
}
```

Add an optional `inputRef` to `codemcp_execute`. When supplied, the referenced JSON value is exposed to sandbox code as `input`; the model can filter or aggregate it without repeating the original MCP calls.

The refinement cache is:

- explicit rather than implicit “last result” state;
- memory-only and scoped to one sidecar;
- cleared on sidecar shutdown;
- bounded by entry count, per-entry bytes, and total bytes;
- protected by a short TTL;
- never persisted in telemetry or session-independent storage.

The implemented defaults are eight entries, 1 MiB per entry, 4 MiB total, and a 300-second TTL.

If an oversized value cannot be retained within the cache bounds, return the existing bounded shape diagnostics without a reference.

## Non-goals

- Raising the default result or Pi output limits.
- Silently truncating or automatically summarizing results.
- Weakening generated types or introducing `Any` as an ergonomic fallback.
- Retrying arbitrary MCP calls automatically.
- Adding cross-service rollback or transaction semantics.
- Persisting execution payloads or creating a general artifact subsystem.
- Adding compatibility writes to both JSON and SQLite telemetry stores.

## Implementation slices

1. `fix: make telemetry process-safe and traceable`
2. `feat: add typed sandbox helpers and exact constraints`
3. `fix: recover upstream clients after connection failures`
4. `feat: refine oversized results without repeating MCP calls`

Each slice must remain independently reviewable and preserve the existing model-facing behavior outside its stated scope.

Implemented as independently reviewable commits:

1. `19bfd9b fix: make telemetry process-safe and traceable`
2. `8952ac2 feat: add typed sandbox helpers and exact constraints`
3. `7c8f504 fix: recover dead upstream connections without replay`
4. `2fba0e4 feat: refine oversized results without repeating calls`

`173a54d docs: steer executions away from raw payloads` adds the model-facing filtering guidance required by slice four.

Verification is executable through `just check`; the implementation checkpoint passes 86 Python tests and 29 TypeScript tests. The regression coverage includes ten-process SQLite merging, trace propagation from a Pi tool call into failure telemetry, preflight/runtime narrowing behavior, dead-client recovery without replay, and retained-result expiry/eviction/cross-sidecar/shutdown bounds.

## Acceptance criteria

### Telemetry

- Ten concurrent sidecar processes produce exact additive operation, failure, histogram, server, and tool counts.
- Lifetime counters never decrease across snapshots.
- A crashed or stale sidecar cannot overwrite newer counters.
- Recent failure references remain bounded at 200 rows.
- A failure trace ID can locate its matching Pi tool result without storing payload content.
- Tests assert that code, arguments, queries, results, error text, and credentials are absent from telemetry.

### Typed sandbox

- Invalid literal `inspect_json` bounds fail preflight with zero upstream calls.
- Each `expect_*` helper narrows its return type during type checking and rejects the wrong runtime shape clearly.
- The generated prelude states the exact supported capability surface.
- Existing valid programs continue to type-check and execute unchanged.

### Connection recovery

- An injected closed connection fails the current call exactly once and does not replay it.
- The cached dead client is discarded.
- The next explicit call creates a fresh client and can succeed.
- Permission, validation, and semantic query errors do not trigger reconnection behavior.

### Oversized-result refinement

- A retainable oversized value returns bounded diagnostics and an opaque reference, never the full value.
- A follow-up execution can consume the reference and return a compact result with zero repeated upstream calls.
- Expired, unknown, cross-sidecar, and evicted references fail explicitly.
- Cache entry, byte, TTL, and shutdown bounds are covered by tests.
- Values above the cache ceiling retain the current shape-only failure behavior.

## Evaluation

After the first three implementation slices, rerun a sanitized regression corpus derived from the observed failure classes and compare:

- first-attempt execution success;
- repeated failures per user task;
- upstream calls completed before a local failure;
- transport recovery on the next explicit attempt;
- answer correctness and tool-output size.

Raw failure count is not the sole quality metric: preflight rejection is preferable to the same error after side effects, and upstream authorization or semantic failures must remain explicit.

The sanitized automated regression corpus now covers each observed class: typed-sandbox rejection, oversized refinement, invalid upstream/permission failures, dead transport recovery, and cancellation. Production before/after comparisons start with telemetry schema version 2 because the untrustworthy historical JSON counters are intentionally not imported.
