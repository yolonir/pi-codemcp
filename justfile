set shell := ["bash", "-euo", "pipefail", "-c"]
set quiet

default: check

lock:
    bun install
    uv lock --project sidecar

sync-typescript:
    bun install --frozen-lockfile

sync-python:
    uv sync --project sidecar --frozen --all-groups

sync: sync-typescript sync-python

init: sync
    uv run --project sidecar prek install

format: format-typescript format-python

format-typescript:
    bun run format

format-python:
    uv run --project sidecar ruff format sidecar tests/python tests/fixtures
    uv run --project sidecar ruff check --fix sidecar tests/python tests/fixtures

check-locks:
    @printf '[1/2] Bun lockfile\n'
    bun install --frozen-lockfile
    @printf '[2/2] uv lockfile\n'
    uv lock --project sidecar --check

typecheck-typescript:
    bun run typecheck

lint-typescript:
    bun run lint

test-typescript:
    bun run test:ts

check-typescript: typecheck-typescript lint-typescript test-typescript

format-check-python:
    uv run --project sidecar ruff format --check sidecar tests/python tests/fixtures

lint-python:
    uv run --project sidecar ruff check sidecar tests/python tests/fixtures

typecheck-python:
    uv run --project sidecar mypy \
        sidecar tests/python/test_executor.py tests/python/test_settings.py
    uv run --project sidecar ty check \
        sidecar tests/python/test_executor.py tests/python/test_settings.py

test-python:
    uv run --project sidecar pytest tests/python -q

check-python: format-check-python lint-python typecheck-python test-python

check-type-suppressions:
    @if git grep -n -E \
        'type:[[:space:]]*ignore|pyright:[[:space:]]*ignore|@ts-(ignore|expect-error|nocheck)|noqa:.*TC[0-9]+' \
        -- '*.py' '*.ts' '*.tsx'; then \
        printf '%s\n' 'Type-check suppression comments are not allowed.' >&2; \
        exit 1; \
    fi

check: check-locks check-typescript check-python check-type-suppressions

release-check:
    #!/usr/bin/env bash
    set -euo pipefail
    temporary="$(mktemp -d "${TMPDIR:-/tmp}/pi-codemcp-release.XXXXXX")"
    cleanup() {
        chmod -R u+w "$temporary" 2>/dev/null || true
        rm -rf "$temporary"
    }
    trap cleanup EXIT
    npm pack --pack-destination "$temporary" >/dev/null
    tarballs=("$temporary"/pi-codemcp-*.tgz)
    [[ ${#tarballs[@]} -eq 1 && -f "${tarballs[0]}" ]]
    npm install --prefix "$temporary/consumer" --omit=dev --legacy-peer-deps \
        "${tarballs[0]}" >/dev/null
    package_root="$temporary/consumer/node_modules/pi-codemcp"
    [[ -f "$package_root/extensions/index.ts" ]]
    [[ -f "$package_root/sidecar/uv.lock" ]]
    [[ -f "$package_root/.python-version" ]]
    [[ ! -e "$package_root/tests" ]]
    [[ ! -e "$package_root/.github" ]]
    if find "$package_root/sidecar" -type d -name __pycache__ -print -quit | grep -q .; then
        echo 'release contains Python bytecode caches' >&2
        exit 1
    fi
    if grep -R -I -n -E '(/Users/|/home/[^/]+/|[A-Za-z]:\\Users\\)' \
        "$package_root/extensions" "$package_root/src" "$package_root/sidecar"; then
        echo 'release contains a machine-specific absolute path' >&2
        exit 1
    fi
    chmod -R a-w "$package_root"
    bun_path="$(command -v bun)"
    env PATH="/usr/bin:/bin" \
        UV_CACHE_DIR="$temporary/uv-cache" \
        UV_PYTHON_INSTALL_DIR="$temporary/uv-python" \
        "$bun_path" tests/release/package-smoke.ts "$package_root" "$temporary/agent"

precommit:
    uv run --project sidecar prek run --all-files

alias quality-check := check
