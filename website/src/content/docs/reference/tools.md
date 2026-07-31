---
title: Pi tools
description: Reference for the five tools exposed by pi-codemcp.
tableOfContents:
  minHeadingLevel: 2
  maxHeadingLevel: 3
---

pi-codemcp exposes five stable tools to Pi. Upstream MCP tool schemas are discovered through these tools rather than added directly to the model context.

## `codemcp_search`

Search capabilities or page through compact inventory.

| Parameter | Type | Notes |
| --- | --- | --- |
| `query` | string, optional | Capability words for search mode. Omit for inventory mode. |
| `mode` | `search` or `inventory`, optional | Defaults to `search`. |
| `detail` | `names`, `signatures`, or `full`, optional | Defaults to `signatures`. Prefer inspect for selected full stubs. |
| `limit` | integer, optional | 1–20; defaults to 5. |
| `cursor` | integer, optional | Non-negative cursor returned by a previous page. |
| `server` | string, optional | Exact configured server name, or `chains` for saved chains. |

The result includes matches, ranking evidence, pagination, catalog scope, discovery failures, and current execution limits.

## `codemcp_inspect`

Return exact typed SDK stubs for selected search results.

| Parameter | Type | Notes |
| --- | --- | --- |
| `calls` | string[] | 1–20 exact call identifiers such as `grafana.query_prometheus`. |

Batch related calls in one inspection. The response deduplicates shared generated types and helpers.

## `codemcp_execute`

Type-check and execute one bounded Python call graph.

| Parameter | Type | Notes |
| --- | --- | --- |
| `code` | string | Sandboxed Python body using prebound typed SDK facades. |
| `inputRef` | string, optional | Opaque retained-result reference exposed to the code as `input`. |

Return a compact final value. Filter, aggregate, join, or sample upstream results inside the sandbox instead of returning raw payloads to the model.

## `codemcp_save_chain`

Validate and persist a reusable typed chain.

| Parameter | Type | Notes |
| --- | --- | --- |
| `scope` | `project` or `global`, optional | Defaults to `project`. |
| `name` | string | Lowercase identifier, 1–64 characters. |
| `description` | string | Purpose and appropriate usage, up to 1,000 characters. |
| `code` | string | Sandboxed body that reads arguments from `input`. |
| `inputSchema` | JSON Schema | Root must be an object. |
| `outputSchema` | JSON Schema | Required contract for the returned value. |

Saving immediately registers `mcp_chain_<name>` as a native Pi tool and `chains.<name>` inside Code Mode.

## `codemcp_manage_chains`

List or explicitly mutate saved chains.

| Parameter | Type | Notes |
| --- | --- | --- |
| `action` | `list`, `enable`, `disable`, `revalidate`, or `delete` | Required. |
| `name` | string, optional | Required for mutations. |
| `scope` | `project` or `global`, optional | Required for mutations. |
| `confirmedByUser` | boolean, optional | Must be true for every mutation after explicit approval. |

Listing is read-only and does not require confirmation.
