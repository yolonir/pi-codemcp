---
title: Getting started
description: Install pi-codemcp, configure an MCP server, and run your first Code Mode workflow.
---

## Requirements

Package users need:

- Pi running on Node.js 22.19 or newer.
- At least one MCP server configured in Pi's `mcp.json`.
- Network access on the first sidecar bootstrap unless the required runtime is already cached.

You do **not** need a system installation of Python, uv, Bun, or just. pi-codemcp uses a packaged uv binary to bootstrap its locked Python 3.13 runtime under Pi's writable agent directory.

## Install the extension

```shell
pi install npm:pi-codemcp
```

Restart Pi or run `/reload` after installation. Open `/codemcp` to view configured servers, saved chains, per-tool policy, cache state, execution limits, and bounded telemetry.

## Configure a server

pi-codemcp reads Pi's existing `<agent-dir>/mcp.json`. Both a wrapped `mcpServers` object and a root server map are accepted.

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

Remote Streamable HTTP, SSE, bearer authentication, and FastMCP-managed OAuth are also supported:

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
  }
}
```

:::caution
Keep credentials in environment variables or your provider's OAuth store. Do not commit bearer tokens or other secrets to `mcp.json`.
:::

## Run your first workflow

Ask Pi for a capability rather than naming an upstream tool you have not inspected:

```text
Find my open issues, group them by priority, and return only the issue identifiers and titles.
```

The normal agent flow is:

1. `codemcp_search` discovers matching calls.
2. `codemcp_inspect` loads any exact contracts not already returned by search.
3. `codemcp_execute` type-checks and runs a compact call graph.
4. The sandbox returns only the filtered or aggregated value requested.

Continue with the [Code Mode workflow](guides/code-mode/) guide for examples and execution rules.
