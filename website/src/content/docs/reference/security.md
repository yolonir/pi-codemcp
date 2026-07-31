---
title: Security and limits
description: Understand pi-codemcp's sandbox boundary, resource limits, and upstream side effects.
---

## Trust boundary

pi-codemcp is a trusted Pi extension and runs with Pi's process permissions. The agent-written Python body does not.

Pydantic Monty type-checks and executes that body without host filesystem, environment, network, subprocess, dynamic imports, or arbitrary package access. Code can interact only with the typed MCP and saved-chain facades exposed in its generated stubs.

FastMCP owns MCP transports, runtime validation, and OAuth.

## Enforced limits

The sidecar enforces:

- An overall execution deadline.
- A per-upstream-tool timeout.
- A total MCP and nested-chain call budget.
- A fixed sandbox memory ceiling.
- A maximum final result size.
- Per-server disabled-tool policy.

The Pi layer separately truncates rendered output according to `outputLimitKiB`. The full oversized rendered value is not persisted.

Executions are serialized per Pi session. Catalogs are cached independently per server and invalidated independently.

## Upstream permissions and side effects

:::danger
Sandboxing agent code does not make upstream MCP calls safe, read-only, or transactional.
:::

Every enabled MCP tool keeps its upstream permissions. A chain never bypasses server authentication, per-tool policy, disabled state, execution limits, or dependency validation.

If an execution makes several mutating calls and a later call fails, pi-codemcp does not roll back the earlier side effects. Preserve a model turn for user approvals and semantic decisions before invoking mutating operations.

## Failure and retry behavior

Failures are explicit and include stable details such as server, tool, retryability, status, and message when available.

pi-codemcp does not silently retry or switch to compatibility behavior. If a connection dies, the failed call is not replayed. The dead connection is evicted, and the next explicit call reconnects.

## Credentials

- Keep secrets out of saved-chain manifests; manifests store only code and schemas.
- Use FastMCP-managed OAuth or environment-variable interpolation for remote authentication.
- Do not embed bearer tokens in committed `mcp.json` files.
- Keep the allowlists for stdio child environments as narrow as possible.
