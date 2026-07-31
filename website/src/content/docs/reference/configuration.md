---
title: Configuration
description: MCP server and runtime settings accepted by pi-codemcp.
---

## MCP servers

pi-codemcp reads Pi's existing `<agent-dir>/mcp.json`. It accepts either a wrapped `mcpServers` object or a root server map.

### Stdio

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

For stdio servers, pi-codemcp passes a small safe base environment plus variables named in `MY_PI_CHILD_ENV_ALLOWLIST` or `MY_PI_MCP_ENV_ALLOWLIST`. Explicit `env` values in the server definition are also passed.

### Streamable HTTP and SSE

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

Remote headers can interpolate allowlisted environment variables with `${NAME}`. FastMCP owns transports, runtime validation, and OAuth.

Pi-only fields including `directTools`, `lifecycle`, `idleTimeout`, `enabled`, and `disabled` are understood locally and are not forwarded to FastMCP.

## Runtime settings

Settings live at `<agent-dir>/pi-codemcp/settings.json` and can also be edited through `/codemcp`:

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

| Setting | Purpose |
| --- | --- |
| `backgroundWarmup` | Begin sidecar and catalog warmup after Pi starts without blocking startup. |
| `cacheTtlHours` | Lifetime of each server's cached tool catalog. |
| `executionTimeoutSeconds` | Overall deadline for one sandbox execution. |
| `toolTimeoutSeconds` | Per-upstream-tool deadline. |
| `maxCalls` | Total upstream and nested-chain call budget. |
| `resultLimitKiB` | Maximum final sandbox result size. |
| `outputLimitKiB` | Maximum rendered Pi tool output size. |
| `disabledTools` | Per-server upstream calls blocked by local policy. |

Version-one settings are migrated when loaded. The removed `outputLineLimit` field is omitted on the next save.

## Management UI

Open `/codemcp` to manage:

- Servers and their discovery state.
- Saved project and global chains.
- Per-tool policy.
- Catalog caches.
- Execution and output limits.
- Bounded lifetime and recent telemetry.

Configuration changes made in the UI are persisted immediately. Discovery, chain revalidation, and chain deletion remain explicit immediate actions.
