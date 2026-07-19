from __future__ import annotations

import hashlib
import os
import time
from contextlib import suppress
from typing import TYPE_CHECKING, Literal

from mcp import types as mcp_types
from pydantic import BaseModel, ConfigDict, ValidationError

from .json_types import JSON_OBJECT_ADAPTER, JsonObject

if TYPE_CHECKING:
    from pathlib import Path

CACHE_VERSION: Literal[1] = 1
DEFAULT_MAX_AGE_SECONDS = 24 * 60 * 60


class CachedServerCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal[1] = CACHE_VERSION
    server_name: str
    config_fingerprint: str
    updated_at: float
    tools: list[JsonObject]


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
        with suppress(OSError):
            self.directory.chmod(0o700)
        path = self._path(server_name)
        temporary = self.directory / f".{path.stem}.{os.getpid()}.{time.time_ns()}.tmp"
        entry = CachedServerCatalog(
            server_name=server_name,
            config_fingerprint=config_fingerprint,
            updated_at=time.time(),
            tools=[
                JSON_OBJECT_ADAPTER.validate_python(
                    tool.model_dump(mode="json", by_alias=True, exclude_none=True)
                )
                for tool in tools
            ],
        )
        temporary.write_text(entry.model_dump_json(), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)

    def _path(self, server_name: str) -> Path:
        digest = hashlib.sha256(server_name.encode("utf-8")).hexdigest()[:20]
        return self.directory / f"{digest}.json"
