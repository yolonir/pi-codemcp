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

Commits and releases:

- Use Conventional Commit titles for PRs and squash commits: `fix:` for a patch, `feat:` for a minor, and `feat!:` or `BREAKING CHANGE:` for a major. Use `chore:`, `docs:`, or `test:` for changes that should not normally release the package.
- Merge ordinary branches into `main` only through a green PR. This updates or creates the Release Please PR but does not publish a release.
- Treat the Release Please PR (`chore(main): release X.Y.Z`) as the explicit release boundary. Merge it only when the user explicitly says to release that version now.
- Never infer permission to tag, create a GitHub Release, or publish to npm from permission to configure release automation, merge a feature PR, or prepare a version.
- A release is complete only after the release PR is merged, exact-main Quality succeeds, and the Release workflow creates the tag/GitHub Release and publishes the npm package.
