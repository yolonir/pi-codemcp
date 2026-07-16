from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Any, Literal

from mcp import types as mcp_types
from pydantic import BaseModel, ValidationError

CACHE_VERSION = 1
DEFAULT_MAX_AGE_SECONDS = 24 * 60 * 60


class CachedServerCatalog(BaseModel):
    version: Literal[1] = CACHE_VERSION
    server_name: str
    config_fingerprint: str
    updated_at: float
    tools: list[dict[str, Any]]


class CatalogCache:
    def __init__(
        self,
        directory: Path,
        *,
        max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
    ) -> None:
        self.directory = directory
        self.max_age_seconds = max_age_seconds

    def load(
        self,
        server_name: str,
        config_fingerprint: str,
    ) -> list[mcp_types.Tool] | None:
        path = self._path(server_name)
        try:
            entry = CachedServerCatalog.model_validate_json(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValidationError, ValueError):
            return None
        if entry.server_name != server_name:
            return None
        if entry.config_fingerprint != config_fingerprint:
            return None
        if time.time() - entry.updated_at > self.max_age_seconds:
            return None
        try:
            return [mcp_types.Tool.model_validate(tool) for tool in entry.tools]
        except ValidationError:
            return None

    def save(
        self,
        server_name: str,
        config_fingerprint: str,
        tools: list[mcp_types.Tool],
    ) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.directory, 0o700)
        except OSError:
            pass
        path = self._path(server_name)
        temporary = path.with_suffix(".tmp")
        entry = CachedServerCatalog(
            server_name=server_name,
            config_fingerprint=config_fingerprint,
            updated_at=time.time(),
            tools=[
                tool.model_dump(mode="json", by_alias=True, exclude_none=True)
                for tool in tools
            ],
        )
        temporary.write_text(entry.model_dump_json(), encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(path)

    def _path(self, server_name: str) -> Path:
        digest = hashlib.sha256(server_name.encode("utf-8")).hexdigest()[:20]
        return self.directory / f"{digest}.json"
