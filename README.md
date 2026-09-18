# pi-codemcp

Typed, sandboxed **Code Mode for your MCP servers in Pi**.

The agent discovers the tools it needs, writes a Python program, and runs dependent or parallel calls across MCP servers. Intermediate data stays in the sandbox; only the program's compact return value goes back to the model.

## Install

```bash
pi install npm:pi-codemcp
```

Reads `<agent-dir>/mcp.json`. Supports stdio, Streamable HTTP, SSE, bearer authentication, and FastMCP-managed OAuth.

Open **`/codemcp`** to manage servers, individual tools, saved chains, settings, and usage stats. Changes are saved immediately. Tool output expands with Pi's normal `Ctrl+O`.

No separate Python, uv, Bun, or just installation is needed. The package bootstraps a locked Python 3.13 runtime on first use; this needs network access unless already cached. Pi startup does not wait for MCP connections.

## How it works

```text
Discover tools → load selected contracts → execute a program → return compact data
```

| Tool | Purpose |
| --- | --- |
| `codemcp_search` | Search capabilities or browse the compact catalog. |
| `codemcp_route` | Optional Jev routing: select tools, roles, and composition guidance. |
| `codemcp_inspect` | Load exact typed SDK contracts for selected calls. |
| `codemcp_execute` | Type-check and run a sandboxed Python program. |
| `codemcp_edit` | Patch the previous program and rerun it. |
| `codemcp_save_chain` | Save a tested program as a reusable tool, with user approval. |
| `codemcp_manage_chains` | List chains; explicitly confirm enable, disable, revalidate, or delete. |

Independent calls can use `asyncio.gather`; dependent calls pass earlier outputs into later inputs. Filtering and aggregation happen inside the sandbox, without model round trips for every intermediate result.

## Optional Jev routing

Provide `TYPESAFE_API_KEY`, then set **Enable Jev → true** in `/codemcp` → Settings. This replaces `codemcp_search` with `codemcp_route` as the agent's discovery tool.

1. The agent calls `codemcp_route()` when it needs MCP capabilities. The extension reads the latest user request and up to three preceding user/assistant messages automatically.
2. Enabled tool names and descriptions go to Jev in parallel chunks of up to 40. Jev scores relevance, assigns workflow roles, and checks whether an intermediate model decision or user approval is needed.
3. CodeMCP ranks and filters the answers, selects at most eight tools, derives composition guidance, and fetches their exact typed contracts.
4. The agent writes the actual `codemcp_execute` program, keeping a checkpoint between stages when needed.

Jev does not execute tools or block ordinary messages. **Routing sends your request, recent context, and tool descriptions to TypeSafe**, so it is opt-in. Without a key, local search stays active; a failed route enables search as a fallback. The SDK also honors `TYPESAFE_BASE_URL` and `TYPESAFE_DEFAULT_MODEL`.

## MCP configuration

Use an `mcpServers` object or a root-level server map:

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
    },
    "linear": {
      "type": "http",
      "url": "https://mcp.linear.app/mcp",
      "auth": "oauth"
    }
  }
}
```

Set `disabled: true` or `enabled: false` to disable a server. For stdio, only a safe base environment, comma-separated variables in `MY_PI_CHILD_ENV_ALLOWLIST` / `MY_PI_MCP_ENV_ALLOWLIST`, and explicit server `env` values are passed through. Remote `headers` can reference allowed environment variables with `${NAME}`.

## Settings

Edit settings in `/codemcp` or `<agent-dir>/pi-codemcp/settings.json`. Defaults: 30-second execution and tool timeouts, 50 upstream calls, 16 KiB returned data, 50 KiB rendered output, and a 24-hour catalog cache.

<details>
<summary>Settings JSON</summary>

```json
{
  "version": 2,
  "jevEnabled": false,
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

Older settings are migrated on load, including `discoveryMode` → `jevEnabled`.

</details>

## Saved chains

With your approval, a successfully executed program can be saved with explicit input/output JSON Schemas. One manifest exposes both:

```text
mcp_chain_weekly_digest(...)   # native Pi tool
chains.weekly_digest(...)     # typed call inside another CodeMCP program
```

Chains can call MCP tools or other chains. Nested calls share time and call budgets; inputs and outputs are validated. Changed dependency contracts mark chains stale for revalidation.

- **Project:** `<project>/.pi/pi-codemcp/chains` (activated in trusted projects).
- **Global:** `<agent-dir>/pi-codemcp/chains`, when explicitly requested.

Project chains override same-named global chains, even when disabled. Manifests store code and schemas, not credentials or execution results. Manage them through `/codemcp`.

## Execution and safety

[FastMCP](https://github.com/jlowin/fastmcp) handles transports, validation, and OAuth. [Pydantic Monty](https://github.com/pydantic/monty) type-checks and executes Python without host filesystem, environment, network, or subprocess access. External access is only through the exposed MCP and saved-chain calls.

- Type errors stop execution **before any upstream call**. Time, memory, call count, and output size are bounded.
- Enabled tools retain their upstream permissions. Chains cannot bypass server or per-tool policy.
- No automatic call retries or cross-service rollback: a later failure does not undo earlier side effects.
- `codemcp_edit` reruns the **whole program**, including upstream calls; it is not a continuation.
- Unscoped search returns available results plus explicit `discovery_failures`; server-scoped search fails if that server is unavailable.

<details>
<summary>Working with unknown or oversized results</summary>

Incomplete schemas use recursive `JsonValue`, not `Any`. Use the prebound `expect_object`, `expect_list`, `expect_string`, and `expect_integer` helpers to narrow values. `inspect_json(value, samples=2, max_depth=3)` returns a bounded structural summary.

Oversized results fail explicitly. If the value fits the in-memory refinement cache, the error includes a `result_ref`. Pass it as `inputRef` in a follow-up `codemcp_execute` to filter the retained value as `input` without repeating upstream calls. References expire after five minutes and are valid only in the originating sidecar.

Failures distinguish `preflight`, `runtime`, `timeout`, `cancelled`, and oversized `result` stages. Rendered output has a separate `outputLimitKiB` cap.

</details>

## Development

```bash
just init           # locked dependencies and hooks
just check          # locks, lint, types, and tests
just release-check  # also verify the packed clean-install path
pi -ne -e . --no-session
```

Uses Bun and uv with Python 3.13. For sidecar debugging:

```bash
uv run --project sidecar --frozen -m sidecar.cli doctor --agent-dir ~/.pi/agent
uv run --project sidecar --frozen -m sidecar.cli search "linear issues"
uv run --project sidecar --frozen -m sidecar.cli execute --code-file plan.py
```

Conventional Commit titles drive Release Please. Feature PRs do not publish; merging the release PR triggers the verified npm release.

## Feedback and credits

Use **Extension is broken!** in `/codemcp` → Settings to ask the agent to investigate and prepare an issue, or [open one directly](https://github.com/yolonir/pi-codemcp/issues). Include the error and version details, with credentials and private data removed.

Inspired by [Cloudflare's Code Mode](https://blog.cloudflare.com/code-mode-mcp/). Independent implementation for Pi using FastMCP and Pydantic Monty.

MIT
