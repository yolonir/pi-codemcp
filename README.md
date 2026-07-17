# pi-codemcp

A Pi package that turns every enabled server in `~/.pi/agent/mcp.json` into three model-facing tools:

- `codemcp_search`
- `codemcp_execute`
- `codemcp_save_chain`

FastMCP owns upstream MCP transports, validation, and OAuth. Pydantic Monty type-checks and runs model-authored Python in a sandbox. Intermediate MCP results remain inside the sandbox instead of entering the model context.

## Install

End users only need Pi:

```bash
pi install npm:pi-codemcp@0.1.0
```

A platform-specific uv 0.8.13 binary ships through the pinned `@manzt/uv-*` npm mirrors of
Astral's release for macOS, Linux, and Windows on x64/arm64 (plus Windows ia32). During the first
non-blocking session warmup, uv installs Python 3.13 and the locked runtime-only Python environment
under Pi's writable agent directory. Python,
uv, Bun, and just do not need to be installed separately by package users.

The first call requires network access unless the uv/Python caches are already populated. When
`PI_OFFLINE` is enabled, pi-codemcp passes offline mode to uv and fails explicitly if the required
artifacts are not cached.

For local development and installation:

```bash
cd pi-codemcp
just init
pi install .
```

For isolated testing without installation:

```bash
pi -ne -e . --no-session
```

Pi starts warming the Python sidecar in the background on `session_start`; startup does not await it. Opening `/codemcp` or making a Code Mode call reuses that same startup and only waits if warmup is still in progress. Warmup reads config and valid catalog caches but never connects an upstream. Each upstream has its own lazy client and is connected only when its catalog must be discovered or one of its tools is executed. Connected stdio children are stopped on Pi session shutdown.

## MCP configuration

The package obtains Pi's agent directory through the official `getAgentDir()` API and reads its
existing `mcp.json`. The default is `~/.pi/agent/mcp.json`; custom `PI_CODING_AGENT_DIR` values are
respected automatically. Users do not need to set pi-codemcp-specific environment variables.
Enabled stdio, Streamable HTTP, and SSE entries are supported. `disabled: true` and
`enabled: false` entries are ignored. Stdio children receive Pi's restricted baseline environment,
explicit per-server `env`, and ambient names allowed through `MY_PI_MCP_ENV_ALLOWLIST` or
`MY_PI_CHILD_ENV_ALLOWLIST`. `${NAME}` references in HTTP headers use that same restricted
environment.

Linear uses FastMCP's native OAuth client directly:

```json
{
  "mcpServers": {
    "linear": {
      "type": "http",
      "url": "https://mcp.linear.app/mcp",
      "auth": "oauth"
    }
  }
}
```

The first Linear discovery or execution opens one browser authorization. FastMCP persists the
registration and tokens under `<agent-dir>/pi-codemcp/oauth` and handles refreshes. The package does
not implement OAuth or import tokens from another client.

Per-server tool catalogs are cached under `<agent-dir>/pi-codemcp/catalog`. The managed uv binary
and Python environment live under `<agent-dir>/pi-codemcp/runtime`; the installed npm package is
never modified at runtime. A valid catalog cache makes search and typed SDK stubs available without
starting upstream servers. Cache entries expire after 24 hours by default and are invalidated
independently when that server's configuration changes; cache files contain tool metadata, never MCP
credentials.

Product settings and persistent per-tool policy are stored under
`<agent-dir>/pi-codemcp/settings.json`. Reusable chain manifests are stored as private JSON files
under `<agent-dir>/pi-codemcp/chains`. They contain sandboxed code and schemas, never credentials or
execution results. Both are managed through `/codemcp`; users do not need to edit them directly.

## Model workflow

1. Call `codemcp_search` with a capability query, optionally scoped to one configured `server`. Each match includes its complete typed SDK stub.
2. Call `codemcp_execute` with the returned facade, such as `await linear.list_issues(arguments)`, and a top-level `return`.
3. When the user explicitly asks to reuse an execution, call `codemcp_save_chain` with parameterized code plus exact input and output JSON Schemas.

Example execution body:

```python
issues = await linear.list_issues({"assignee": "me"})
identifiers = []
for issue in issues:
    identifiers.append(issue["identifier"])
return {"issues": identifiers}
```

Before any upstream call, Monty checks tool names, required arguments, represented JSON Schema
types, and structured output usage. FastMCP and the upstream server still perform runtime
validation. When an upstream omits schema information, the facade uses recursive `JsonValue`
instead of `Any`; model code must narrow it with `isinstance` before typed field access.

## Saved chains

A saved chain is exposed through the same manifest in two places:

- `mcp_chain_<name>` as an immediately active native Pi tool;
- `chains.<name>(arguments)` inside `codemcp_execute` and other saved chains.

Saved code reads its native arguments from the typed `input` object and must return a value matching
its required output schema. Saving an existing name updates and re-enables it. Saved chains appear in
`codemcp_search` with `source: "saved_chain"`, so an agent can discover and compose them exactly like
upstream SDK calls.

Chains may call other chains or recursively call themselves. Nested calls execute inside the same
sandboxed execution graph rather than re-entering the Pi harness. They share one catalog snapshot,
deadline, cancellation signal, and total call budget. A hard recursion-depth guard prevents infinite
self-calls; each nested input and result is runtime-validated against its manifest contract. Schema
fingerprints mark dependents stale when a referenced tool or chain contract changes.

## Limits

- 30-second execution timeout by default (configurable in `/codemcp`)
- 30-second per-tool timeout by default (configurable)
- 100 MB Monty heap limit
- 50 total upstream-tool and nested-chain calls per execution by default (configurable)
- one execution at a time per Pi session
- no sandbox access to host files, environment, network, or subprocesses
- returned value must be smaller than 16 KiB by default; oversized values fail with a compact shape summary
- no retries or cross-service rollback

Every enabled upstream tool is available by default, including mutations. Individual tools can be disabled persistently in `/codemcp`; disabled tools are removed from search, typed stubs, and execution. Permissions for enabled tools come from upstream credentials and servers.

## TUI

```text
/codemcp
```

Opens a split dashboard with **Servers**, **Chains**, and **Settings** tabs. The Servers tab keeps the compact server list on the left and selected-server details, actions, and per-tool toggles on the right. Wide terminals also show the selected tool's name and wrapped description in a separate card. Enabling a server immediately discovers its catalog. Press `d` to force-refresh it later. Server changes are saved atomically to Pi's existing `mcp.json`; per-tool policy and settings are saved under `<agent-dir>/pi-codemcp/settings.json`.

The Chains tab lists each saved native tool and shows its description, input/output contract, direct server/tool/chain dependencies, reverse callers, and stale dependencies. `space` enables or disables the selected chain, `r` revalidates it against the current catalog, and `del` removes it when no other chain depends on it.

The Settings tab controls background warmup, catalog TTL, execution and per-tool timeouts, maximum calls, final-result KiB, and agent-visible output KiB/line limits. Internal bootstrap and IPC safety timeouts, recursion depth, and the sandbox memory ceiling are intentionally not user-configurable.

Code Mode tool calls render a compact summary and short preview by default. Successful summaries show an approximate token count for the agent-visible output using Pi's conservative characters/4 heuristic. Use Pi's standard `Ctrl+O` expansion to show separate **Agent code** and **Output** sections. Successful executions show only the returned value; failures distinguish preflight errors (code was not run and no MCP calls occurred) from runtime errors (including the number of calls already made).

## Development

Development requires Bun 1.3.10, uv 0.8.13, just, and Python 3.13. Install locked dependencies and
repository hooks:

```bash
just init
```

Run the complete local/CI quality gate:

```bash
just check
```

This verifies both lockfiles, TypeScript with `tsc`, Biome, Bun tests, Ruff formatting and linting,
mypy, ty, and pytest. `just release-check` additionally packs the npm tarball, installs it into a
clean consumer directory, starts it without a system uv on `PATH`, and verifies package contents and
runtime placement. Direct commits to `main` are rejected by the installed `prek` hook. See
[CONTRIBUTING.md](CONTRIBUTING.md) for the branch and pull-request workflow.
