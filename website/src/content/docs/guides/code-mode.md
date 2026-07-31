---
title: Code Mode workflow
description: Discover, inspect, and execute typed MCP calls through pi-codemcp.
---

Code Mode keeps schemas and intermediate data out of the model context until they are actually needed.

## 1. Search for capabilities

Use capability words rather than guessing a tool name:

```text
codemcp_search({ query: "issues assigned to me" })
```

The default `signatures` detail includes exact stubs for up to three top matches and compact alternatives. Search results include ranking evidence, pagination, server scope, and execution limits.

Use inventory mode when you need to browse rather than rank:

```text
codemcp_search({ mode: "inventory", limit: 20 })
```

An unscoped search discovers configured servers independently. Available catalogs still return results when another server is unavailable, and `discovery_failures` names the failures. A server-scoped search remains fail-fast.

## 2. Inspect selected contracts

Search already returns a small number of exact stubs. Load additional selected contracts in one call:

```text
codemcp_inspect({ calls: ["linear.list_issues", "linear.get_issue"] })
```

Inspection deduplicates the shared `JsonValue` and helper prelude. Avoid loading schemas that the execution plan will not use.

## 3. Execute inside the sandbox

Submit one bounded Python body:

```python
issues = await linear.list_issues({"assignee": "me", "limit": 50})
return {
    "count": len(issues),
    "issues": [
        {"id": issue["identifier"], "title": issue["title"]}
        for issue in issues
    ],
}
```

SDK facades are prebound globals and must not be imported. The sandbox allows `import asyncio` for `asyncio.gather`, but has no host filesystem, environment, network, subprocess, dynamic imports, or arbitrary packages.

Use `asyncio.gather` for independent calls:

```python
import asyncio

issues, projects = await asyncio.gather(
    linear.list_issues({"assignee": "me", "limit": 50}),
    linear.list_projects({"limit": 50}),
)
return {"issues": len(issues), "projects": len(projects)}
```

Keep semantic decisions in a model turn. Code Mode is best for deterministic filtering, joining, aggregation, and sampling—not for hiding an approval or judgment inside code.

## Unknown and oversized values

Incomplete upstream schemas become recursive `JsonValue`, not `Any`. Narrow them explicitly with the prebound helpers:

- `expect_object`
- `expect_list`
- `expect_string`
- `expect_integer`

For unfamiliar output, use `inspect_json(value, samples=2, max_depth=3)` to return a bounded structural summary.

If the final result exceeds `resultLimitKiB`, execution fails explicitly with shape and sample diagnostics. When possible, the failure also includes an opaque `result_ref`. Pass it back as `inputRef` in one follow-up execution to refine the retained value without repeating upstream calls:

```python
rows = expect_list(input)
return [row for row in rows if expect_object(row).get("status") == "open"][:20]
```

References expire after five minutes, stay in the originating sidecar, and are never persisted.

## Failure stages

Execution reports where it failed:

- `preflight`: code did not run and no upstream call was made.
- `runtime`: the sandbox or an upstream call failed after execution began.
- `timeout` or `cancelled`: execution was stopped.
- `result`: execution completed, but the returned value exceeded its result limit.

There are no automatic retries. A dead upstream connection is evicted after the original call fails; the next explicit call reconnects.
