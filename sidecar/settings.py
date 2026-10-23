from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .executor import ExecutionSettings

if TYPE_CHECKING:
    from pathlib import Path


def _camel_case(name: str) -> str:
    head, *tail = name.split("_")
    alias = head + "".join(part.capitalize() for part in tail)
    return f"{alias[:-3]}KiB" if alias.endswith("Kib") else alias


class CodeMcpProfile(BaseModel):
    model_config = ConfigDict(
        alias_generator=_camel_case, populate_by_name=True, extra="forbid", strict=True
    )

    servers: list[str]
    disabled_tools: dict[str, list[str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_servers(self) -> CodeMcpProfile:
        if any(not server for server in self.servers):
            raise ValueError("Profile servers must be non-empty names")
        return self


class CodeMcpSettings(BaseModel):
    model_config = ConfigDict(
        alias_generator=_camel_case,
        populate_by_name=True,
        extra="forbid",
        strict=True,
    )

    version: Literal[2] = 2
    jev_enabled: bool = False
    background_warmup: bool = True
    cache_ttl_hours: int = Field(default=24, ge=0, le=720)
    execution_timeout_seconds: int = Field(default=30, ge=1, le=300)
    tool_timeout_seconds: int = Field(default=30, ge=1, le=300)
    max_calls: int = Field(default=50, ge=1, le=200)
    result_limit_kib: int = Field(default=16, ge=1, le=1024)
    output_limit_kib: int = Field(default=50, ge=1, le=1024)
    disabled_tools: dict[str, list[str]] = Field(default_factory=dict)
    profiles: dict[str, CodeMcpProfile] = Field(default_factory=dict)
    active_profile: str | None = None

    @model_validator(mode="after")
    def validate_profiles(self) -> CodeMcpSettings:
        for name in self.profiles:
            if (
                not name.strip()
                or name != name.strip()
                or re.fullmatch(r"[^\x00-\x1f\x7f]{1,80}", name) is None
            ):
                raise ValueError(
                    "Profile names must be 1-80 characters without "
                    "surrounding whitespace or control characters"
                )
        if self.active_profile is not None and self.active_profile not in self.profiles:
            raise ValueError(f"Unknown CodeMCP profile: {self.active_profile}")
        return self

    @property
    def enabled_servers(self) -> set[str] | None:
        if self.active_profile is None:
            return None
        return set(self.profiles[self.active_profile].servers)

    def tool_enabled(self, server: str, tool: str) -> bool:
        policy = self.disabled_tools
        if self.active_profile is not None:
            profile = self.profiles[self.active_profile]
            if server not in profile.servers:
                return False
            policy = profile.disabled_tools
        return tool not in policy.get(server, ())

    def execution_settings(self) -> ExecutionSettings:
        return ExecutionSettings(
            timeout_seconds=self.execution_timeout_seconds,
            max_calls=self.max_calls,
            tool_timeout_seconds=self.tool_timeout_seconds,
            result_byte_limit=self.result_limit_kib * 1024,
        )

    @property
    def cache_ttl_seconds(self) -> int:
        return self.cache_ttl_hours * 60 * 60


def load_settings(path: Path) -> CodeMcpSettings:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return CodeMcpSettings()
    value = json.loads(raw)
    if isinstance(value, dict) and value.get("version", 1) == 1:
        value = {key: item for key, item in value.items() if key != "outputLineLimit"}
        value["version"] = 2
    if isinstance(value, dict) and "discoveryMode" in value:
        mode = value.pop("discoveryMode")
        if not isinstance(mode, str) or mode not in {"search", "jev"}:
            value["discoveryMode"] = mode
        else:
            value.setdefault("jevEnabled", mode == "jev")
    return CodeMcpSettings.model_validate(value)
