# pi-mcp-codemode

A Pi package that turns every enabled server in `~/.pi/agent/mcp.json` into three model-facing tools:

- `codemode_search`
- `codemode_get_schema`
- `codemode_execute`

FastMCP owns upstream MCP transports, validation, and OAuth. Pydantic Monty type-checks and runs model-authored Python in a sandbox. Intermediate MCP results remain inside the sandbox instead of entering the model context.

## Requirements

- [Bun](https://bun.sh/)
- [uv](https://docs.astral.sh/uv/)
- Pi

## Install locally

```bash
cd pi-mcp-codemode
bun install
uv sync --project sidecar --frozen
pi install .
```

For isolated testing without installation:

```bash
pi -ne -e . --no-session
```

The sidecar starts lazily on the first Code Mode tool call or `/codemode`. Opening status never connects an upstream. Each upstream has its own lazy client and is connected only when its catalog must be discovered or one of its tools is executed. Connected stdio children are stopped on Pi session shutdown.

## MCP configuration

The package reads the existing `~/.pi/agent/mcp.json`. Enabled stdio, Streamable HTTP, and SSE entries are supported. `disabled: true` entries are ignored.

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

The first Linear discovery or execution opens one browser authorization. FastMCP persists the registration and tokens under `~/.pi/agent/pi-mcp-codemode/oauth` and handles refreshes. The package does not implement OAuth or import tokens from another client.

Per-server tool catalogs are cached under `~/.pi/agent/pi-mcp-codemode/catalog`. A valid cache makes search and schema lookup available without starting upstream servers. Cache entries expire after 24 hours and are invalidated independently when that server's configuration changes; cache files contain tool metadata, never MCP credentials.

## Model workflow

1. Call `codemode_search` with a capability query, optionally scoped to one configured `server`.
2. Call `codemode_get_schema` for the selected exact tool names.
3. Call `codemode_execute` with the typed SDK facade shown in the schema, such as `await linear.list_issues(arguments)`, and a top-level `return`.

Example execution body:

```python
issues = await linear.list_issues({"assignee": "me"})
identifiers = []
for issue in issues:
    identifiers.append(issue["identifier"])
return {"issues": identifiers}
```

Before any upstream call, Monty checks tool names, required arguments, represented JSON Schema types, and structured output usage. FastMCP and the upstream server still perform runtime validation.

## Limits

- 30-second execution timeout
- 100 MB Monty heap limit
- 50 upstream calls per execution
- one execution at a time per Pi session
- no sandbox access to host files, environment, network, or subprocesses
- returned value must be smaller than 16 KiB; oversized values fail with a compact shape summary
- no retries or cross-service rollback

Every enabled upstream tool is available, including mutations. Permissions come from upstream credentials and servers.

## TUI

```text
/codemode
```

Opens a minimal read-only modal with connected upstreams, transports, authentication modes, and tool counts.

Code Mode tool calls render a compact summary and short preview by default. Use Pi's standard `Ctrl+O` expansion to show separate **Agent code** and **Output** sections. Successful executions show only the returned value; failures distinguish preflight errors (code was not run and no MCP calls occurred) from runtime errors (including the number of calls already made).

## Development

```bash
bun run check
```

This runs TypeScript type-checking, Biome, Bun integration tests, and the uv/pytest sidecar suite.
