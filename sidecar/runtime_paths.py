from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_AGENT_DIR = Path.home() / ".pi" / "agent"
CODEMCP_AGENT_DIR_ENV = "PI_CODEMCP_AGENT_DIR"
CODEMCP_PROJECT_CHAINS_DIR_ENV = "PI_CODEMCP_PROJECT_CHAINS_DIR"
PI_AGENT_DIR_ENV = "PI_CODING_AGENT_DIR"


@dataclass(frozen=True)
class RuntimePaths:
    config_path: Path
    oauth_dir: Path
    catalog_dir: Path
    settings_path: Path
    global_chains_dir: Path
    project_chains_dir: Path | None

    def as_tuple(self) -> tuple[Path, Path, Path, Path, Path, Path | None]:
        return (
            self.config_path,
            self.oauth_dir,
            self.catalog_dir,
            self.settings_path,
            self.global_chains_dir,
            self.project_chains_dir,
        )


def resolve_runtime_paths(
    *,
    agent_dir: Path | str | None = None,
    config_path: Path | str | None = None,
    settings_path: Path | str | None = None,
    oauth_dir: Path | str | None = None,
    catalog_dir: Path | str | None = None,
    global_chains_dir: Path | str | None = None,
    project_chains_dir: Path | str | None = None,
) -> RuntimePaths:
    resolved_agent_dir = _agent_dir(agent_dir)
    state_dir = resolved_agent_dir / "pi-codemcp"
    return RuntimePaths(
        config_path=_resolve_path(config_path) or resolved_agent_dir / "mcp.json",
        oauth_dir=_resolve_path(oauth_dir) or state_dir / "oauth",
        catalog_dir=_resolve_path(catalog_dir) or state_dir / "catalog",
        settings_path=_resolve_path(settings_path) or state_dir / "settings.json",
        global_chains_dir=_resolve_path(global_chains_dir) or state_dir / "chains",
        project_chains_dir=_project_chains_dir(project_chains_dir),
    )


def _agent_dir(value: Path | str | None) -> Path:
    if value is not None:
        return _resolve_path(value) or DEFAULT_AGENT_DIR
    raw_agent_dir = os.environ.get(CODEMCP_AGENT_DIR_ENV) or os.environ.get(PI_AGENT_DIR_ENV)
    return _resolve_path(raw_agent_dir) or DEFAULT_AGENT_DIR


def _project_chains_dir(value: Path | str | None) -> Path | None:
    if value is not None:
        return _resolve_path(value)
    return _resolve_path(os.environ.get(CODEMCP_PROJECT_CHAINS_DIR_ENV))


def _resolve_path(value: Path | str | None) -> Path | None:
    if value is None:
        return None
    return Path(value).expanduser()
