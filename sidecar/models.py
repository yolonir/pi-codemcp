from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer

type ServerTransport = Literal["stdio", "http", "sse"]
type ServerAuth = Literal["oauth", "bearer"]


class NormalizedServerInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str
    transport: ServerTransport
    config_fingerprint: str
    enabled: bool = True
    auth: ServerAuth | None = None
    description: str | None = None


class ToolSchemaView(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str
    call: str
    source: Literal["mcp_tool", "saved_chain"]
    server: str | None = None
    description: str | None = None
    signature: str
    stub: str

    @model_serializer(mode="plain")
    def serialize_compact(self) -> dict[str, object]:
        values: dict[str, object] = {
            "name": self.name,
            "call": self.call,
            "source": self.source,
        }
        if self.server is not None:
            values["server"] = self.server
        if self.description is not None:
            values["description"] = self.description
        values["signature"] = self.signature
        values["stub"] = self.stub
        return values


class ServerToolSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str
    tool_count: int


class ExecutionLimitsView(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    timeout_seconds: float
    tool_timeout_seconds: float
    max_calls: int
    result_limit_bytes: int


class SearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    total_tool_count: int
    servers: list[ServerToolSummary]
    project_scope_available: bool
    execution_limits: ExecutionLimitsView
    prelude: str
    results: list[ToolSchemaView]


class UpstreamToolStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str
    enabled: bool = True
    description: str | None = None


class UpstreamStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str
    transport: ServerTransport
    enabled: bool = True
    connected: bool = False
    discovered: bool = False
    auth: ServerAuth | None = None
    tool_count: int = 0
    total_tool_count: int = 0
    tools: list[UpstreamToolStatus] = Field(default_factory=list)


class StatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    connected: bool
    config_path: str
    tool_count: int = 0
    upstreams: list[UpstreamStatus] = Field(default_factory=list)
