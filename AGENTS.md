# pi-codemcp

Pi package providing typed, sandboxed Code Mode access to configured MCP servers.

- TypeScript dependencies, scripts, and tests use Bun.
- The Python sidecar and tests use uv with the committed `sidecar/uv.lock`.
- Pi loads `extensions/index.ts` directly; there is no build step.
- Keep MCP transports and OAuth in FastMCP and sandbox execution in Pydantic Monty.

Repository workflow:

- Run `just init` once to sync locked dependencies and install the `prek` hook.
- Work on a focused branch; direct commits to `main` are rejected.
- Run `just check` for the same lock, lint, type, and test gates used by CI.
- Run `just release-check` before release to validate the packed clean-install path.
- Use `just format` for safe automatic formatting and lint fixes.
