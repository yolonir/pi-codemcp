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

## MCP configuration examples

`pi-codemcp` reads the same MCP config Pi uses. Either shape is accepted:

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
    }
  }
}
```

or a root server map:

```json
{
  "linear": {
    "type": "http",
    "url": "https://mcp.linear.app/mcp",
    "auth": "oauth"
  },
  "grafana": {
    "type": "sse",
    "url": "https://grafana.example.com/sse",
    "headers": {
      "authorization": "Bearer ${GRAFANA_MCP_TOKEN}"
    }
  },
  "disabled-example": {
    "command": "example-server",
    "disabled": true
  }
}
```

For stdio servers, pi-codemcp passes a small safe base environment plus variables listed in `MY_PI_CHILD_ENV_ALLOWLIST` or `MY_PI_MCP_ENV_ALLOWLIST`. Explicit `env` values in `mcp.json` are also passed. Remote headers can reference allowed environment variables with `${NAME}`. Pi-only fields such as `directTools`, `lifecycle`, `idleTimeout`, `enabled`, and `disabled` are understood locally and are not forwarded to FastMCP.

## Settings JSON

Settings live at `<agent-dir>/pi-codemcp/settings.json` and can also be edited in `/codemcp`:

```json
{
  "version": 2,
  "backgroundWarmup": true,
  "cacheTtlHours": 24,
  "executionTimeoutSeconds": 30,
  "toolTimeoutSeconds": 30,
  "maxCalls": 50,
  "resultLimitKiB": 16,
  "outputLimitKiB": 50,
  "disabledTools": {
    "linear": ["delete_issue"]
  }
}
```

The Python sidecar enforces catalog cache TTL, execution timeout, per-tool timeout, max MCP calls, result size, and disabled-tool policy. The TypeScript Pi layer uses `backgroundWarmup` and `outputLimitKiB` for session warmup and rendered-output truncation; the sidecar still validates those fields so the settings file has one strict shared schema. Version-one files are migrated when loaded, and the removed `outputLineLimit` field is omitted on the next save.

## Search and execute flow

The agent searches for a capability, inspects the selected exact stub when needed, and executes a compact plan. Unscoped searches discover stale or missing server catalogs independently: available servers still return results, while `discovery_failures` explicitly reports unavailable servers. Server-scoped searches remain fail-fast.

```python
issues = await linear.list_issues({"assignee": "me", "limit": 50})
return {"count": len(issues), "ids": [issue["identifier"] for issue in issues]}
```

The same flow is available through the internal CLI for debugging:

```bash
uv run --project sidecar --frozen -m sidecar.cli search "issues assigned to me"
uv run --project sidecar --frozen -m sidecar.cli execute --code-file plan.py
```

A direct one-shot plan can call multiple servers without model round trips between calls:

```bash
uv run --project sidecar --frozen -m sidecar.cli execute --code '
number = await alpha.get_number({"seed": 41})
saved = await beta.save_number({"value": number["value"]})
return {"number": number["value"], "identifier": saved["identifier"]}
'
```

Incomplete upstream schemas become recursive `JsonValue`, not `Any`; use the prebound `expect_object`, `expect_list`, `expect_string`, and `expect_integer` helpers to narrow unknown values explicitly. For unfamiliar outputs, `inspect_json(value, samples=2, max_depth=3)` returns a byte-bounded structural summary, cardinality, field sizes, and samples; `samples` is limited to 1–3 and `max_depth` to 1–6 during preflight. The generated prelude documents the sandbox surface: use `import asyncio` with `asyncio.gather`; unavailable host or stdlib APIs are rejected. Preflight type errors happen before any upstream call is made, and oversized final results fail explicitly with the same actionable inspection data.

## Saved-chain CLI flow

Saved chains are JSON manifests with sandboxed code plus explicit input/output JSON Schemas. Project-scoped chains live under `<project>/.pi/pi-codemcp/chains`; global chains live under `<agent-dir>/pi-codemcp/chains`.

```bash
uv run --project sidecar --frozen -m sidecar.cli chain save save_number \
  --description "Fetch and save one generated number." \
  --code 'number = await alpha.get_number({"seed": input["seed"]})
return await beta.save_number({"value": number["value"]})' \
  --input-schema '{"type":"object","properties":{"seed":{"type":"integer"}},"required":["seed"],"additionalProperties":false}' \
  --output-schema '{"type":"object","properties":{"saved":{"type":"boolean"},"identifier":{"type":"string"}},"required":["saved","identifier"],"additionalProperties":false}'

uv run --project sidecar --frozen -m sidecar.cli chain list
uv run --project sidecar --frozen -m sidecar.cli chain run save_number --input '{"seed":41}'
uv run --project sidecar --frozen -m sidecar.cli chain revalidate save_number --scope project
uv run --project sidecar --frozen -m sidecar.cli chain delete save_number --scope project
```

Revalidation checks the saved code against the current enabled catalog. Deletion refuses to remove chains still referenced by other chains. Disabling a project chain does not fall back to a same-named global chain; project scope continues to shadow global scope until the project manifest is deleted.

## Output and result normalization

When an upstream tool declares an output schema, pi-codemcp requires `structuredContent`, validates it, dumps it back to JSON-compatible values, and preserves declared structured string fields as strings. FastMCP-wrapped `result` strings are intentionally unwrapped and parsed because those wrappers commonly carry JSON payloads as text. When no output schema exists, single text responses that look like JSON objects, arrays, `null`, `true`, or `false` are normalized into native JSON values; non-JSON text remains a string.

Execution results report explicit stages:

- `preflight`: code did not run and no upstream call was made.
- `runtime`: the sandbox or an upstream call failed after execution started.
- `timeout` / `cancelled`: execution was stopped.
- `result`: the call graph completed, but the returned value exceeded `resultLimitKiB`.

Rendered Pi output is separately truncated by `outputLimitKiB`; the full oversized rendered value is not persisted.

## Safety and limits

FastMCP owns MCP transports, runtime validation, and OAuth. [Pydantic Monty](https://github.com/pydantic/monty) type-checks and executes agent-written Python without host filesystem, environment, network, or subprocess access. Code Mode can only call the typed MCP tool and saved-chain facades exposed in the generated stubs.

`/codemcp` configures servers, saved chains, per-tool policy, timeouts, call limits, output limits, cache TTL, and warmup, and shows bounded lifetime/recent telemetry in its Stats tab. Server, chain, tool-policy, and setting changes are persisted immediately. Discovery, revalidation, and deletion remain explicit immediate actions. The sandbox also has a fixed memory ceiling; executions are serialized per Pi session. There are no automatic retries or cross-service rollback.

Enabled tools retain their upstream permissions. Saved chains never bypass server or per-tool policy and are checked against the current enabled catalog whenever they run. Preflight safety does not make upstream tools transactional: if a later call fails after earlier calls succeeded, pi-codemcp does not roll those upstream side effects back.

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

Development and packaged runtime checks target Python 3.13. The sidecar metadata, `.python-version`, mypy, ty, and CI all align on that version.

To test the checkout without loading an installed copy:

```bash
pi -ne -e . --no-session
```

The sidecar also has a stable internal CLI for development, debugging, and future runtime adapters:

```bash
uv run --project sidecar --frozen -m sidecar.cli serve --stdio
uv run --project sidecar --frozen -m sidecar.cli status --agent-dir ~/.pi/agent
uv run --project sidecar --frozen -m sidecar.cli search "linear issues"
uv run --project sidecar --frozen -m sidecar.cli execute --code-file plan.py
uv run --project sidecar --frozen -m sidecar.cli chain list
uv run --project sidecar --frozen -m sidecar.cli doctor --agent-dir ~/.pi/agent
```

`just check` runs lockfile checks, TypeScript, Biome, Bun tests, Ruff, mypy, ty, and pytest. `just release-check` additionally packs the npm artifact, installs it into a clean consumer directory, and runs the packaged sidecar without a system uv on `PATH`.

## Releases

Release Please derives versions and release notes from Conventional Commit titles on `main`: `fix:` publishes a patch, `feat:` publishes a minor, and a `!` or `BREAKING CHANGE:` publishes a major. It maintains the release PR, `CHANGELOG.md`, `package.json`, version tag, and GitHub Release.

Merging a release PR publishes the verified package to npm from `.github/workflows/release.yml` using trusted publishing and provenance. Quality gates the exact merge commit on Linux, macOS, and Windows; the publish job checks out its release tag, packs it with Bun, and uses npm only for the final OIDC-authenticated upload.

## Credits

The core search-and-execute philosophy is inspired by Cloudflare's Code Mode work. pi-codemcp is an independent implementation for Pi that composes arbitrary configured MCP servers through FastMCP and a Pydantic Monty sandbox.

MIT
