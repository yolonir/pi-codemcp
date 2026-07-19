# pi-codemcp

Fast, typed, sandboxed **Code Mode for every MCP server configured in Pi**.

Instead of putting every upstream MCP tool definition into the model context, pi-codemcp gives the agent a small interface for discovery, execution, and reuse:

- `codemcp_search` ranks capabilities or pages through a compact inventory without loading full schemas.
- `codemcp_inspect` returns exact typed SDK stubs only for selected calls.
- `codemcp_execute` runs one sandboxed Python call graph across one or many MCP servers.
- `codemcp_save_chain` turns a repeated call graph into a reusable native Pi tool.
- `codemcp_manage_chains` lists chains or performs an explicitly confirmed enable, disable, revalidate, or delete.

Intermediate results stay inside the sandbox. The model receives only the compact value returned by the program.

## Why Code Mode?

MCP has an uncomfortable scaling property: the more tools an agent can use, the more tool schemas compete with the actual task for context. Multi-step work also tends to bounce every intermediate result through the model, adding tokens, latency, and opportunities for mistakes.

Cloudflare described a better pattern in [Code Mode: give agents an entire API in 1,000 tokens](https://blog.cloudflare.com/code-mode-mcp/): expose a small search-and-execute surface, let the model write code against a typed SDK, and execute that code in a sandbox. Their work reports a fixed tool footprint and dramatic context savings for very large APIs. The open-source implementation lives in [`@cloudflare/codemode`](https://github.com/cloudflare/agents/tree/main/packages/codemode).

pi-codemcp applies that idea on the **client side** to the MCP servers you already use in Pi:

1. Search the combined catalog or page through a compact inventory.
2. Inspect exact schemas only for the calls selected for the task.
3. Type-check a compact Python plan before any upstream call happens.
4. Execute dependent or parallel calls without model round-trips between them.
5. Return only the final data the agent actually needs.
6. Save stable plans as native tools and reuse them without rewriting the call graph.

That can make complex MCP workflows faster and substantially more token-efficient. Exact savings depend on the servers, schemas, model, and task.

## Built for daily use, not a demo

I built this because I care a lot about software that is genuinely fast, efficient, and predictable enough to use every day. Too many AI extensions look good in a short demo but become slow, noisy, fragile, or effectively unusable in real work.

pi-codemcp is deliberately opinionated about operational quality:

- Pi startup does not wait for Python or MCP servers.
- Each upstream connection is lazy and independent.
- Tool catalogs are cached per server and invalidated independently.
- Agent-written code is type-checked before execution.
- Time, memory, call count, and output size are bounded.
- Failures are explicit; there are no silent retries or compatibility fallbacks.
- Tool output is compact by default and expands with Pi's normal `Ctrl+O` UI.
- Bounded local telemetry uses fixed rollups rather than session event logs and appears in the `/codemcp` Stats tab.

There is always room to make it faster and more reliable. If something is not working well, please report it rather than silently giving up on the extension.

## Saved MCP chains

Any successful MCP call graph can become a reusable tool with an explicit input and output JSON Schema.

A saved chain is exposed in two forms from one manifest:

```text
mcp_chain_weekly_digest(...)   # native Pi tool
chains.weekly_digest(...)      # typed call inside Code Mode
```

Chains can call upstream MCP tools, other saved chains, or themselves recursively. This enables reusable composition such as:

```python
issues = await chains.collect_open_issues({"assignee": input["assignee"]})
result = await slack.post_message({
    "channel": input["channel"],
    "text": issues["summary"],
})
return {"posted": result["ok"], "count": issues["count"]}
```

Nested chains share the same deadline, cancellation signal, catalog snapshot, and total call budget. Every nested input and output is runtime-validated. Recursion is supported but bounded. Dependency fingerprints mark chains stale when a referenced contract changes.

New manifests default to project scope under `<project>/.pi/pi-codemcp/chains`; explicitly global chains live under `<agent-dir>/pi-codemcp/chains`. A project chain overrides a same-named global chain without deleting it. Manifests contain sandboxed code and schemas, never credentials or execution results. `/codemcp` labels both scopes and can revalidate, enable, disable, or delete chains.

There is deliberately no implicit “save last execution” state: the agent must submit the exact successfully tested code plus explicit input and output contracts. This keeps persistence reviewable and avoids saving the wrong attempt from a long session.

## Install

```bash
pi install npm:pi-codemcp
```

It reads Pi's existing `<agent-dir>/mcp.json` and supports stdio, Streamable HTTP, SSE, bearer authentication, and FastMCP-managed OAuth. Open `/codemcp` to manage servers, per-tool policy, saved chains, cache, and execution limits.

Package users do not need Python, uv, Bun, or just. A pinned uv binary bootstraps the locked Python 3.13 runtime under Pi's writable agent directory on first use; the first bootstrap needs network access unless already cached.

## Agent workflow

The agent searches for a capability, inspects the selected exact stub when needed, and executes a compact plan:

```python
issues = await linear.list_issues({"assignee": "me", "limit": 50})
return {"count": len(issues), "ids": [issue["identifier"] for issue in issues]}
```

Incomplete upstream schemas become recursive `JsonValue`, not `Any`; unknown values must be narrowed explicitly before typed use. For unfamiliar outputs, `inspect_json(value, samples=2, max_depth=3)` returns a bounded structural summary, cardinality, field sizes, and samples. Oversized final results fail explicitly with the same actionable inspection data.

## Safety and limits

FastMCP owns MCP transports, runtime validation, and OAuth. [Pydantic Monty](https://github.com/pydantic/monty) type-checks and executes agent-written Python without host filesystem, environment, network, or subprocess access.

`/codemcp` configures servers, saved chains, per-tool policy, timeouts, call limits, output limits, cache TTL, and warmup, and shows bounded lifetime/recent telemetry in its Stats tab. Server, chain, tool-policy, and setting toggles stay local and instantaneous until one `Ctrl+S` batch save/reload. Discovery, revalidation, and deletion remain explicit immediate actions. The sandbox also has a fixed memory ceiling; executions are serialized per Pi session. There are no automatic retries or cross-service rollback.

Enabled tools retain their upstream permissions. Saved chains never bypass server or per-tool policy and are checked against the current enabled catalog whenever they run.

## Something failed? Please open an issue

Please do not assume your failure is too specific or not worth reporting. Platform differences, strange schemas, slow startup, confusing rendering, OAuth problems, and rough edges are exactly the reports that make this project better.

Open an issue at <https://github.com/yolonir/pi-codemcp/issues>.

You can ask your coding agent to do the work:

```text
Reproduce this pi-codemcp problem, redact all credentials and private data,
collect the pi-codemcp version, Pi version, OS/architecture, MCP transport,
minimal configuration shape, exact error, and relevant logs, then open a
GitHub issue at https://github.com/yolonir/pi-codemcp/issues.
```

If the agent cannot create the issue, ask it to prepare the title and body for you. I would much rather receive an incomplete report than have someone hit a problem, abandon the package, and never say anything. I will read the issues and work through them.

## Local development

```bash
just init
just check
just release-check
```

To test the checkout without loading an installed copy:

```bash
pi -ne -e . --no-session
```

`just check` runs lockfile checks, TypeScript, Biome, Bun tests, Ruff, mypy, ty, and pytest. `just release-check` additionally packs the npm artifact, installs it into a clean consumer directory, and runs the packaged sidecar without a system uv on `PATH`.

## Releases

Release Please derives versions and release notes from Conventional Commit titles on `main`: `fix:` publishes a patch, `feat:` publishes a minor, and a `!` or `BREAKING CHANGE:` publishes a major. It maintains the release PR, `CHANGELOG.md`, `package.json`, version tag, and GitHub Release.

Merging a release PR publishes the verified package to npm from `.github/workflows/release.yml` using trusted publishing and provenance. Quality gates the exact merge commit on Linux, macOS, and Windows; the publish job checks out its release tag, packs it with Bun, and uses npm only for the final OIDC-authenticated upload.

## Credits

The core search-and-execute philosophy is inspired by Cloudflare's Code Mode work. pi-codemcp is an independent implementation for Pi that composes arbitrary configured MCP servers through FastMCP and a Pydantic Monty sandbox.

MIT
