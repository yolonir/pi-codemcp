from __future__ import annotations

import json
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from .executor import ExecutionSettings

if TYPE_CHECKING:
    from pathlib import Path


def _camel_case(name: str) -> str:
    head, *tail = name.split("_")
    alias = head + "".join(part.capitalize() for part in tail)
    return f"{alias[:-3]}KiB" if alias.endswith("Kib") else alias


class CodeMcpSettings(BaseModel):
    model_config = ConfigDict(
        alias_generator=_camel_case,
        populate_by_name=True,
        extra="forbid",
        strict=True,
    )

    version: Literal[2] = 2
    background_warmup: bool = True
    cache_ttl_hours: int = Field(default=24, ge=0, le=720)
    execution_timeout_seconds: int = Field(default=30, ge=1, le=300)
    tool_timeout_seconds: int = Field(default=30, ge=1, le=300)
    max_calls: int = Field(default=50, ge=1, le=200)
    result_limit_kib: int = Field(default=16, ge=1, le=1024)
    output_limit_kib: int = Field(default=50, ge=1, le=1024)
    disabled_tools: dict[str, list[str]] = Field(default_factory=dict)

    def tool_enabled(self, server: str, tool: str) -> bool:
        return tool not in self.disabled_tools.get(server, ())

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
    return CodeMcpSettings.model_validate(value)
