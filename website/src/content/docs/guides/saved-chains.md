---
title: Saved chains
description: Turn a successful MCP call graph into a reusable typed Pi tool.
---

A saved chain persists sandboxed code plus explicit input and output JSON Schemas. It is exposed in two forms from one manifest:

```text
mcp_chain_weekly_digest(...)  # native Pi tool
chains.weekly_digest(...)     # typed call inside Code Mode
```

## When to save a chain

Save a chain when:

- The same successfully tested call graph will be reused.
- Its input and output can be described with strict JSON Schemas.
- Its behavior is deterministic enough to expose as a named tool.

Do not save an untested draft. pi-codemcp deliberately has no implicit “save last execution” state; the exact tested code and both schemas must be submitted.

## Save a chain

The agent calls `codemcp_save_chain` with:

- A lowercase `name` matching `^[a-z][a-z0-9_]{0,63}$`.
- A concise `description` explaining when the chain is appropriate.
- The tested sandboxed `code`.
- An object-rooted `inputSchema`.
- A required `outputSchema`.
- Optional `scope`, which defaults to `project`.

Example code body:

```python
number = await alpha.get_number({"seed": input["seed"]})
return await beta.save_number({"value": number["value"]})
```

A chain can call upstream MCP tools, other chains, or itself recursively. Nested calls share the same deadline, cancellation signal, catalog snapshot, and total call budget. Recursion is supported but bounded.

## Project and global scope

Project chains live under:

```text
<project>/.pi/pi-codemcp/chains
```

Explicitly global chains live under:

```text
<agent-dir>/pi-codemcp/chains
```

A project chain overrides a same-named global chain. Disabling the project chain does not fall back to the global one; project scope keeps shadowing it until the project manifest is deleted.

Manifests contain code and schemas, never credentials or execution results.

## Manage chains

`codemcp_manage_chains` can list chains freely. Mutating actions are:

- `enable`
- `disable`
- `revalidate`
- `delete`

Every mutation requires the exact `name`, `scope`, and `confirmedByUser: true` after explicit user approval. Deletion refuses to remove a chain that another chain still references.

Revalidation checks saved code against the current enabled catalog. Dependency fingerprints mark a chain stale when a referenced contract changes.

:::caution
Saved chains do not bypass server permissions, disabled-tool policy, execution limits, or dependency checks. Upstream side effects are not transactional and cannot be rolled back by pi-codemcp.
:::
