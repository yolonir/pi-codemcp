# Contributing

## Setup

Requirements: Bun 1.3.10, uv 0.8.13, just, and Python 3.13.

```bash
just init
```

This installs locked TypeScript and Python dependencies and installs the `prek` pre-commit and pre-push Git hooks.

## Workflow

Direct commits to `main` are rejected by the installed hook. Work on a focused branch:

```bash
git switch -c feature/short-description
```

Before opening a pull request:

```bash
just check
just release-check
```

Use `just format` for safe automatic formatting and lint fixes. Pre-commit runs fast hygiene,
format, and lint checks; pre-push runs the complete quality gate. Do not bypass hooks with
`--no-verify`. GitHub rulesets protect `main`, require pull requests, and require every Quality
check.

## Commands

```text
just sync          Install exactly what the lockfiles specify
just format        Format TypeScript and Python, then apply safe lint fixes
just check         Run lock, lint, type, and test gates
just precommit     Run fast pre-commit hooks against the repository
just prepush       Run the complete pre-push quality gate
just release-check Pack, install, and smoke-test the production npm artifact
```

Tests must not mutate live MCP data. Keep FastMCP responsible for transports and OAuth, and keep
Pydantic Monty responsible for sandbox execution.
