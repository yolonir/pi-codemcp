# pi-mcp-codemode

Pi package providing typed, sandboxed Code Mode access to configured MCP servers.

- TypeScript dependencies, scripts, and tests use Bun.
- The Python sidecar and tests use uv with the committed `sidecar/uv.lock`.
- Pi loads `extensions/index.ts` directly; there is no build step.
- Keep MCP transports and OAuth in FastMCP and sandbox execution in Pydantic Monty.
- Never expose the upstream MCP catalog as individual Pi tools.
- Do not mutate live MCP data in tests.

Validation:

```bash
bun run check
```
