from __future__ import annotations

import hashlib
import json
import keyword
import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastmcp.client.auth import OAuth
from fastmcp.mcp_config import (
    MCPConfig,
    RemoteMCPServer,
    StdioMCPServer,
    infer_transport_type_from_url,
)
from fastmcp.utilities.json_schema_type import json_schema_to_type
from key_value.aio.stores.filetree import FileTreeStore
from key_value.aio.stores.filetree.store import (
    FileTreeV1CollectionSanitizationStrategy,
    FileTreeV1KeySanitizationStrategy,
)
from mcp import types as mcp_types
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_serializer
from pydantic_core import to_jsonable_python
from rapidfuzz import fuzz, process, utils

from .json_types import (
    JSON_OBJECT_ADAPTER,
    JSON_VALUE_ADAPTER,
    JsonObject,
    JsonSchema,
    JsonValue,
)

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    import httpx

PI_ONLY_FIELDS = {"directTools", "lifecycle", "idleTimeout", "disabled", "enabled"}
REMOTE_TRANSPORTS = {"http", "streamable-http", "sse"}
BASE_CHILD_ENV_KEYS = {
    "CI",
    "COLORTERM",
    "FORCE_COLOR",
    "HOME",
    "LANG",
    "LOGNAME",
    "NO_COLOR",
    "PATH",
    "PI_CODING_AGENT_DIR",
    "SHELL",
    "TEMP",
    "TERM",
    "TMP",
    "TMPDIR",
    "USER",
}
ENV_ALLOWLIST_KEYS = ("MY_PI_CHILD_ENV_ALLOWLIST", "MY_PI_MCP_ENV_ALLOWLIST")
ENV_REFERENCE_PATTERN = re.compile(r"\$\{([^}]+)\}")
# A 50-point partial match is generic half-string overlap; require evidence above it.
SEARCH_SCORE_CUTOFF = 51
STUB_IMPORTS = "from typing import Literal, Never, NotRequired, TypeAlias, TypedDict"
JSON_TYPE_STUBS = (
    "JsonScalar: TypeAlias = bool | int | float | str | None",
    'JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]',
)


class NormalizedServerInfo(BaseModel):
    name: str
    transport: str
    config_fingerprint: str
    enabled: bool = True
    auth: str | None = None
    description: str | None = None


class ToolSchemaView(BaseModel):
    name: str
    call: str
    server: str | None = None
    description: str | None = None
    signature: str
    stub: str

    @model_serializer(mode="plain")
    def serialize_compact(self) -> JsonObject:
        values: JsonObject = {
            "name": self.name,
            "call": self.call,
            "signature": self.signature,
            "stub": self.stub,
        }
        if self.server is not None:
            values["server"] = self.server
        if self.description is not None:
            values["description"] = self.description
        return values


class ServerToolSummary(BaseModel):
    name: str
    tool_count: int


class SearchResponse(BaseModel):
    total_tool_count: int
    servers: list[ServerToolSummary]
    results: list[ToolSchemaView]


class UpstreamToolStatus(BaseModel):
    name: str
    enabled: bool = True
    description: str | None = None


class UpstreamStatus(BaseModel):
    name: str
    transport: str
    enabled: bool = True
    connected: bool = False
    discovered: bool = False
    auth: str | None = None
    tool_count: int = 0
    total_tool_count: int = 0
    tools: list[UpstreamToolStatus] = Field(default_factory=list)


class StatusResponse(BaseModel):
    connected: bool
    config_path: str
    tool_count: int = 0
    upstreams: list[UpstreamStatus] = Field(default_factory=list)


class NormalizedConfig(BaseModel):
    config: MCPConfig
    servers: list[NormalizedServerInfo]

    model_config = ConfigDict(arbitrary_types_allowed=True)


@dataclass(slots=True)
class ToolSpec:
    name: str
    backend_name: str
    server: str
    namespace: str
    method: str
    call: str
    external_name: str
    short_name: str
    description: str | None
    input_schema: JsonObject
    output_schema: JsonObject | None
    input_type_name: str
    output_type_name: str
    signature: str
    stub: str
    search_blob: str
    input_adapter: TypeAdapter[object]
    output_adapter: TypeAdapter[object] | None = None
    wrapped_output_adapter: TypeAdapter[object] | None = None
    output_wrap_result: bool = False


@dataclass(slots=True)
class ToolCatalog:
    fingerprint: str
    tools: dict[str, ToolSpec]
    type_stubs: str
    servers: tuple[str, ...]
    server_aliases: dict[str, str]
    facade_calls: dict[tuple[str, str], str]

    @classmethod
    def from_server_tools(
        cls,
        server_tools: dict[str, list[mcp_types.Tool]],
        server_names: Iterable[str] | None = None,
    ) -> ToolCatalog:
        server_name_list = tuple(server_names or server_tools.keys())
        namespace_aliases = _unique_python_aliases(server_name_list)
        prepared: list[tuple[mcp_types.Tool, str, str, str, str]] = []
        for server in server_name_list:
            tools = sorted(server_tools.get(server, []), key=lambda item: item.name)
            method_aliases = _unique_python_aliases(tool.name for tool in tools)
            for tool in tools:
                public_name = f"{server}_{tool.name}"
                prepared.append((
                    tool,
                    public_name,
                    server,
                    namespace_aliases[server],
                    method_aliases[tool.name],
                ))
        return cls._from_prepared(prepared, server_name_list, namespace_aliases)

    @classmethod
    def from_mcp_tools(
        cls,
        tools: Iterable[mcp_types.Tool],
        server_names: Iterable[str],
    ) -> ToolCatalog:
        """Compatibility constructor for already namespaced aggregate catalogs."""
        names = tuple(server_names)
        grouped: dict[str, list[mcp_types.Tool]] = {name: [] for name in names}
        for tool in tools:
            server = _extract_server_name(tool.name, names)
            if server is None:
                if len(names) != 1:
                    raise ValueError(f"Cannot determine server for MCP tool {tool.name!r}")
                server = names[0]
                backend_name = tool.name
            else:
                backend_name = tool.name[len(server) + 1 :]
            grouped[server].append(tool.model_copy(update={"name": backend_name}))
        return cls.from_server_tools(grouped, names)

    @classmethod
    def _from_prepared(
        cls,
        prepared: list[tuple[mcp_types.Tool, str, str, str, str]],
        server_names: tuple[str, ...],
        server_aliases: dict[str, str],
    ) -> ToolCatalog:
        fingerprint_source = [
            {
                "name": public_name,
                "backendName": tool.name,
                "server": server,
                "call": f"{namespace}.{method}",
                "description": tool.description,
                "inputSchema": tool.inputSchema,
                "outputSchema": tool.outputSchema,
            }
            for tool, public_name, server, namespace, method in prepared
        ]
        fingerprint = hashlib.sha256(
            json.dumps(fingerprint_source, sort_keys=True, default=str).encode()
        ).hexdigest()

        specs: dict[str, ToolSpec] = {}
        definitions: list[str] = []
        facade_methods: dict[str, list[str]] = {}
        facade_classes: dict[str, str] = {}
        facade_calls: dict[tuple[str, str], str] = {}

        for tool, public_name, server, namespace, method in prepared:
            if public_name in specs:
                raise ValueError(f"Duplicate MCP tool name after namespacing: {public_name}")
            input_schema = JSON_OBJECT_ADAPTER.validate_python(tool.inputSchema)
            output_schema = (
                JSON_OBJECT_ADAPTER.validate_python(tool.outputSchema)
                if tool.outputSchema is not None
                else None
            )
            builder = StubBuilder(public_name, input_schema, output_schema)
            input_type, output_type, tool_definitions = builder.build()
            call = f"{namespace}.{method}"
            signature = f"await {call}(arguments: {input_type}) -> {output_type}"
            wrap_output = bool(output_schema and output_schema.get("x-fastmcp-wrap-result"))
            adapter_schema: JsonSchema | None = output_schema
            wrapped_schema: JsonSchema | None = None
            if output_schema:
                raw_wrapped_schema = _object_property(output_schema, "result")
                if isinstance(raw_wrapped_schema, (dict, bool)):
                    wrapped_schema = raw_wrapped_schema
            if wrap_output and output_schema:
                adapter_schema = wrapped_schema or True
            external_name = f"__codemcp_{hashlib.sha256(public_name.encode()).hexdigest()[:16]}"

            spec = ToolSpec(
                name=public_name,
                backend_name=tool.name,
                server=server,
                namespace=namespace,
                method=method,
                call=call,
                external_name=external_name,
                short_name=tool.name,
                description=tool.description,
                input_schema=input_schema,
                output_schema=output_schema,
                input_type_name=input_type,
                output_type_name=output_type,
                signature=signature,
                stub="\n\n".join([*JSON_TYPE_STUBS, *tool_definitions]),
                search_blob=_build_search_blob(
                    public_name,
                    call,
                    server,
                    tool.name,
                    tool.description,
                    input_schema,
                ),
                input_adapter=_schema_adapter(input_schema),
                output_adapter=(
                    _schema_adapter(adapter_schema) if adapter_schema is not None else None
                ),
                wrapped_output_adapter=(
                    _schema_adapter(wrapped_schema) if wrapped_schema is not None else None
                ),
                output_wrap_result=wrap_output,
            )
            specs[public_name] = spec
            definitions.extend(tool_definitions)
            class_name = facade_classes.setdefault(
                namespace,
                f"_{_pascal_case(namespace)}Sdk",
            )
            facade_methods.setdefault(namespace, []).append(
                f"    async def {method}(self, arguments: {input_type}) -> {output_type}: ..."
            )
            facade_calls[namespace, method] = public_name

        facade_stubs: list[str] = []
        for namespace in sorted(facade_methods):
            class_name = facade_classes[namespace]
            facade_stubs.extend((
                "\n".join([f"class {class_name}:", *facade_methods[namespace]]),
                f"{namespace}: {class_name}",
            ))

        type_stubs = "\n\n".join([
            STUB_IMPORTS,
            *JSON_TYPE_STUBS,
            *_dedupe(definitions),
            *facade_stubs,
        ])
        return cls(
            fingerprint=fingerprint,
            tools=specs,
            type_stubs=type_stubs,
            servers=server_names,
            server_aliases=server_aliases,
            facade_calls=facade_calls,
        )

    def search(
        self,
        query: str,
        limit: int = 5,
        *,
        server: str | None = None,
    ) -> list[ToolSchemaView]:
        candidates = {
            spec.name: spec.search_blob
            for spec in sorted(self.tools.values(), key=lambda item: item.name)
            if server is None or spec.server == server
        }
        ranked = process.extract(
            query,
            candidates,
            scorer=fuzz.partial_ratio,
            processor=utils.default_process,
            score_cutoff=SEARCH_SCORE_CUTOFF,
            limit=limit,
        )
        return [
            ToolSchemaView(
                name=self.tools[name].name,
                call=self.tools[name].call,
                server=self.tools[name].server,
                description=_short_description(self.tools[name].description),
                signature=self.tools[name].signature,
                stub=self.tools[name].stub,
            )
            for _, _, name in ranked
        ]

    def counts_by_server(self) -> dict[str, int]:
        counts = dict.fromkeys(self.servers, 0)
        for spec in self.tools.values():
            if spec.server:
                counts[spec.server] = counts.get(spec.server, 0) + 1
        return counts

    def validate_arguments(self, tool_name: str, arguments: JsonObject) -> JsonObject:
        spec = self.tools[tool_name]
        value = spec.input_adapter.validate_python(arguments)
        dumped = JSON_VALUE_ADAPTER.validate_python(
            spec.input_adapter.dump_python(
                value,
                mode="json",
                exclude_unset=True,
            )
        )
        if not isinstance(dumped, dict):
            raise TypeError(f"{tool_name}: arguments did not validate as an object")
        projected = _project_to_input_shape(dumped, arguments)
        if not isinstance(projected, dict):
            raise TypeError(f"{tool_name}: arguments did not normalize as an object")
        return projected

    def normalize_result(self, tool_name: str, result: mcp_types.CallToolResult) -> JsonValue:
        spec = self.tools[tool_name]
        if result.isError:
            message = "Upstream MCP tool returned an error"
            if result.content and isinstance(result.content[0], mcp_types.TextContent):
                message = result.content[0].text
            raise RuntimeError(f"{tool_name}: {message}")

        if spec.output_schema:
            if result.structuredContent is None:
                raise RuntimeError(
                    f"{tool_name}: upstream returned no structuredContent "
                    "for its declared output schema"
                )
            structured = JSON_VALUE_ADAPTER.validate_python(result.structuredContent)
            raw_meta: object = (result.meta or {}).get("fastmcp")
            wrap_from_meta = isinstance(raw_meta, dict) and bool(raw_meta.get("wrap_result"))
            if spec.output_wrap_result or wrap_from_meta:
                if not isinstance(structured, dict) or "result" not in structured:
                    raise RuntimeError(f"{tool_name}: wrapped output omitted the result field")
                structured = structured["result"]
            adapter = spec.output_adapter
            if wrap_from_meta and not spec.output_wrap_result:
                adapter = spec.wrapped_output_adapter
            if adapter is None:
                return _normalize_json_value(
                    JSON_VALUE_ADAPTER.validate_python(to_jsonable_python(structured))
                )
            validated = adapter.validate_python(structured)
            return _normalize_json_value(
                JSON_VALUE_ADAPTER.validate_python(adapter.dump_python(validated, mode="json"))
            )

        if result.structuredContent is not None:
            return _normalize_json_value(
                JSON_VALUE_ADAPTER.validate_python(to_jsonable_python(result.structuredContent))
            )
        if len(result.content) == 1 and isinstance(result.content[0], mcp_types.TextContent):
            return _normalize_text_result(result.content[0].text)
        return [
            JSON_OBJECT_ADAPTER.validate_python(block.model_dump(mode="json", by_alias=True))
            for block in result.content
        ]


def _project_to_input_shape(normalized: JsonValue, supplied: JsonValue) -> JsonValue:
    if isinstance(normalized, dict) and isinstance(supplied, dict):
        return {
            key: _project_to_input_shape(normalized[key], supplied_value)
            for key, supplied_value in supplied.items()
            if key in normalized
        }
    if isinstance(normalized, list) and isinstance(supplied, list):
        return [
            _project_to_input_shape(item, supplied[index])
            for index, item in enumerate(normalized)
            if index < len(supplied)
        ]
    return normalized


def _normalize_json_value(value: JsonValue) -> JsonValue:
    if isinstance(value, str):
        return _normalize_text_result(value)
    if isinstance(value, dict):
        return {key: _normalize_json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_json_value(item) for item in value]
    return value


def _normalize_text_result(text: str) -> JsonValue:
    stripped = text.strip()
    if stripped in {"null", "true", "false"}:
        return JSON_VALUE_ADAPTER.validate_json(stripped)
    if not stripped.startswith(("{", "[")):
        return text
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return text
    return (
        _normalize_json_value(JSON_VALUE_ADAPTER.validate_python(parsed))
        if isinstance(parsed, (dict, list))
        else text
    )


def load_mcp_json(path: Path) -> JsonObject:
    if not path.exists():
        raise FileNotFoundError(f"MCP config not found: {path}")
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        raise ValueError(f"MCP config is empty: {path}")
    parsed = JSON_VALUE_ADAPTER.validate_json(raw)
    if not isinstance(parsed, dict):
        raise TypeError("mcp.json root must be an object")
    return parsed


def _string_record(
    value: JsonValue | None,
    *,
    label: str,
    server_name: str,
) -> JsonObject:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise TypeError(f"MCP server {server_name!r} {label} must be an object")
    result: JsonObject = {}
    for key, entry in value.items():
        if not isinstance(entry, str):
            raise TypeError(f"MCP server {server_name!r} {label}.{key} must be a string")
        result[key] = entry
    return result


def _child_process_environment(
    explicit_value: JsonValue | None,
    *,
    server_name: str,
) -> JsonObject:
    allowed_keys = set(BASE_CHILD_ENV_KEYS)
    allowed_keys.update(key for key in os.environ if key.startswith("LC_"))
    for allowlist_key in ENV_ALLOWLIST_KEYS:
        allowed_keys.update(
            key.strip() for key in os.environ.get(allowlist_key, "").split(",") if key.strip()
        )

    environment: JsonObject = {key: os.environ[key] for key in allowed_keys if key in os.environ}
    environment.update(_string_record(explicit_value, label="env", server_name=server_name))
    return environment


def _expanded_headers(value: JsonValue, *, server_name: str) -> JsonObject:
    headers = _string_record(value, label="headers", server_name=server_name)
    environment = _child_process_environment(None, server_name=server_name)

    def replace(match: re.Match[str]) -> str:
        replacement = environment.get(match.group(1))
        return replacement if isinstance(replacement, str) else ""

    return {
        key: ENV_REFERENCE_PATTERN.sub(replace, header)
        for key, header in headers.items()
        if isinstance(header, str)
    }


def _required_string(value: JsonValue | None, *, label: str, server_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError(f"MCP server {server_name!r} {label} must be a non-empty string")
    return value


def _disabled_server_info(
    name: str,
    config: JsonObject,
    config_fingerprint: str,
) -> NormalizedServerInfo:
    if "command" in config:
        _required_string(config.get("command"), label="command", server_name=name)
        return NormalizedServerInfo(
            name=name,
            transport="stdio",
            config_fingerprint=config_fingerprint,
            enabled=False,
        )
    if "url" in config:
        url = _required_string(config.get("url"), label="url", server_name=name)
        transport = config.get("transport") or config.get("type")
        if transport is None:
            transport = infer_transport_type_from_url(url)
        if not isinstance(transport, str) or transport not in REMOTE_TRANSPORTS:
            raise ValueError(f"Unsupported MCP transport for {name}: {transport}")
        raw_auth = config.get("auth")
        auth_kind = (
            "oauth"
            if raw_auth == "oauth"
            else "bearer"
            if isinstance(raw_auth, str) and raw_auth
            else None
        )
        return NormalizedServerInfo(
            name=name,
            transport="sse" if transport == "sse" else "http",
            config_fingerprint=config_fingerprint,
            enabled=False,
            auth=auth_kind,
        )
    raise ValueError(f"MCP server {name!r} must define either command or url")


def normalize_mcp_config(
    raw_config: JsonObject,
    *,
    oauth_storage_dir: Path,
    oauth_client_name: str = "pi-codemcp",
) -> NormalizedConfig:
    server_block = raw_config.get("mcpServers", raw_config)
    if not isinstance(server_block, dict):
        raise TypeError("mcp.json must contain an object at the root or under mcpServers")

    oauth_storage_dir.mkdir(parents=True, exist_ok=True)
    oauth_storage = FileTreeStore(
        data_directory=oauth_storage_dir,
        key_sanitization_strategy=FileTreeV1KeySanitizationStrategy(oauth_storage_dir),
        collection_sanitization_strategy=FileTreeV1CollectionSanitizationStrategy(
            oauth_storage_dir
        ),
    )
    normalized_servers: dict[str, StdioMCPServer | RemoteMCPServer] = {}
    server_infos: list[NormalizedServerInfo] = []

    for name, value in server_block.items():
        if not isinstance(value, dict):
            raise TypeError(f"MCP server {name!r} must be an object")
        cleaned: JsonObject = {
            key: item for key, item in value.items() if key not in PI_ONLY_FIELDS
        }
        config_fingerprint = _server_config_fingerprint(name, cleaned)
        if value.get("disabled") is True or value.get("enabled") is False:
            server_infos.append(_disabled_server_info(name, cleaned, config_fingerprint))
            continue

        if "command" in cleaned:
            cleaned["env"] = _child_process_environment(
                cleaned.get("env"),
                server_name=name,
            )
            stdio_server = StdioMCPServer.model_validate({
                **cleaned,
                "transport": "stdio",
                "type": "stdio",
            })
            normalized_servers[name] = stdio_server
            server_infos.append(
                NormalizedServerInfo(
                    name=name,
                    transport="stdio",
                    config_fingerprint=config_fingerprint,
                    description=stdio_server.description,
                )
            )
            continue

        if "url" in cleaned:
            url = _required_string(cleaned.get("url"), label="url", server_name=name)
            transport = cleaned.get("transport") or cleaned.get("type")
            if transport is None:
                transport = infer_transport_type_from_url(url)
            if not isinstance(transport, str) or transport not in REMOTE_TRANSPORTS:
                raise ValueError(f"Unsupported MCP transport for {name}: {transport}")
            raw_headers = cleaned.get("headers")
            if raw_headers is not None:
                cleaned["headers"] = _expanded_headers(raw_headers, server_name=name)
            raw_auth = cleaned.get("auth")
            auth: str | httpx.Auth | None
            auth_kind: str | None = None
            if raw_auth == "oauth":
                auth = OAuth(
                    mcp_url=url,
                    client_name=oauth_client_name,
                    token_storage=oauth_storage,
                )
                auth_kind = "oauth"
            elif isinstance(raw_auth, str):
                auth = raw_auth or None
                auth_kind = "bearer" if raw_auth else None
            elif raw_auth is None:
                auth = None
            else:
                raise TypeError(f"MCP server {name!r} auth must be a string")
            remote_server = RemoteMCPServer.model_validate({
                **cleaned,
                "transport": transport,
                "auth": auth,
            })
            normalized_servers[name] = remote_server
            server_infos.append(
                NormalizedServerInfo(
                    name=name,
                    transport="sse" if transport == "sse" else "http",
                    config_fingerprint=config_fingerprint,
                    auth=auth_kind,
                    description=remote_server.description,
                )
            )
            continue

        raise ValueError(f"MCP server {name!r} must define either command or url")

    return NormalizedConfig(
        config=MCPConfig(mcpServers=normalized_servers),
        servers=server_infos,
    )


def _as_schema(value: JsonValue | None) -> JsonSchema:
    return value if isinstance(value, (dict, bool)) else True


def _object_property(schema: JsonObject, name: str) -> JsonValue | None:
    properties = schema.get("properties")
    return properties.get(name) if isinstance(properties, dict) else None


def _schema_adapter(schema: JsonSchema) -> TypeAdapter[object]:
    return TypeAdapter(json_schema_to_type(schema))


class StubBuilder:
    def __init__(
        self,
        tool_name: str,
        input_schema: JsonObject,
        output_schema: JsonObject | None,
    ) -> None:
        self.tool_name = tool_name
        self.input_schema = input_schema
        self.output_schema = output_schema
        self._definitions: list[str] = []
        self._seen: dict[tuple[str, str], str] = {}
        self._active_refs: set[str] = set()

    def build(self) -> tuple[str, str, list[str]]:
        input_type = self._ensure_named_type(
            f"{_pascal_case(self.tool_name)}Args",
            self.input_schema,
            self.input_schema,
        )
        output_schema: JsonSchema = self.output_schema or True
        if self.output_schema and self.output_schema.get("x-fastmcp-wrap-result"):
            output_schema = _as_schema(_object_property(self.output_schema, "result"))
        if isinstance(output_schema, dict) and output_schema.get("type") == "string":
            # Runtime normalization promotes JSON object/array text to native values.
            # A plain `str` annotation would therefore be a false guarantee.
            output_schema = True
        output_type = self._ensure_named_type(
            f"{_pascal_case(self.tool_name)}Result",
            output_schema,
            self.output_schema or output_schema,
        )
        return input_type, output_type, _dedupe(self._definitions)

    def _ensure_named_type(
        self,
        name: str,
        schema: JsonSchema,
        root: JsonSchema,
    ) -> str:
        expression = self._type_expr(schema, root, name)
        if expression == name:
            return name
        key = (name, _schema_fingerprint(schema))
        if key not in self._seen:
            self._seen[key] = name
            self._definitions.append(f"{name}: TypeAlias = {expression}")
        return name

    def _type_expr(
        self,
        schema: JsonSchema,
        root: JsonSchema,
        name: str,
    ) -> str:
        if isinstance(schema, bool):
            return "JsonValue" if schema else "Never"
        ref = schema.get("$ref")
        if isinstance(ref, str):
            return self._resolved_ref_type(ref, root, name)
        constant = schema.get("const")
        if isinstance(constant, (bool, int, float, str)) or (
            constant is None and "const" in schema
        ):
            return f"Literal[{constant!r}]"
        enum_values = schema.get("enum")
        if (
            isinstance(enum_values, list)
            and enum_values
            and all(
                isinstance(value, (bool, int, float, str)) or value is None for value in enum_values
            )
        ):
            return f"Literal[{', '.join(repr(value) for value in enum_values)}]"
        alternatives = schema.get("anyOf") or schema.get("oneOf")
        if isinstance(alternatives, list):
            rendered = [
                self._type_expr(member, root, f"{name}Option{index}")
                for index, member in enumerate(alternatives, start=1)
                if isinstance(member, (dict, bool))
            ]
            return " | ".join(dict.fromkeys(rendered)) or "JsonValue"
        if "allOf" in schema:
            merged = _merge_all_of(schema, root)
            return "JsonValue" if merged is None else self._type_expr(merged, root, name)

        raw_type = schema.get("type")
        if isinstance(raw_type, list):
            rendered = [
                self._type_expr({**schema, "type": member}, root, name)
                for member in raw_type
                if isinstance(member, str)
            ]
            return " | ".join(dict.fromkeys(rendered)) or "JsonValue"
        if raw_type is None:
            if "properties" in schema or "additionalProperties" in schema:
                raw_type = "object"
            elif "items" in schema:
                raw_type = "array"
            else:
                return "JsonValue"

        primitives = {
            "string": "str",
            "integer": "int",
            "number": "float",
            "boolean": "bool",
            "null": "None",
        }
        if isinstance(raw_type, str) and raw_type in primitives:
            return primitives[raw_type]
        if raw_type == "array":
            items = schema.get("items", True)
            if isinstance(items, list):
                members = [
                    self._type_expr(
                        _as_schema(item),
                        root,
                        f"{name}Item{index}",
                    )
                    for index, item in enumerate(items, start=1)
                ]
                return f"tuple[{', '.join(members)}]"
            return f"list[{self._type_expr(_as_schema(items), root, f'{name}Item')}]"
        if raw_type == "object":
            raw_properties = schema.get("properties")
            properties = raw_properties if isinstance(raw_properties, dict) else {}
            if properties:
                if any(not _valid_identifier(prop) for prop in properties):
                    return "dict[str, JsonValue]"
                key = (name, _schema_fingerprint(schema))
                if key in self._seen:
                    return self._seen[key]
                self._seen[key] = name
                raw_required = schema.get("required")
                required = (
                    {item for item in raw_required if isinstance(item, str)}
                    if isinstance(raw_required, list)
                    else set()
                )
                lines = [f"class {name}(TypedDict):"]
                for prop, prop_schema in properties.items():
                    child_schema = _as_schema(prop_schema)
                    prop_type = self._type_expr(
                        child_schema,
                        root,
                        f"{name}{_pascal_case(prop)}",
                    )
                    wrapper = prop_type if prop in required else f"NotRequired[{prop_type}]"
                    comment = _field_comment(child_schema)
                    suffix = f"  # {comment}" if comment else ""
                    lines.append(f"    {prop}: {wrapper}{suffix}")
                self._definitions.append("\n".join(lines))
                return name
            additional = _as_schema(schema.get("additionalProperties", True))
            return f"dict[str, {self._type_expr(additional, root, f'{name}Value')}]"
        return "JsonValue"

    def _resolved_ref_type(self, ref: str, root: JsonSchema, name: str) -> str:
        if ref in self._active_refs:
            return "JsonValue"
        self._active_refs.add(ref)
        try:
            return self._type_expr(_resolve_ref(ref, root), root, name)
        finally:
            self._active_refs.remove(ref)


def _short_description(description: str | None, limit: int = 240) -> str | None:
    if description is None:
        return None
    first_paragraph = description.split("\n\n", 1)[0]
    compact = " ".join(first_paragraph.split())
    if len(compact) <= limit:
        return compact
    return f"{compact[: limit - 1].rstrip()}…"


def _field_comment(schema: JsonSchema) -> str | None:
    if isinstance(schema, bool):
        return None
    notes: list[str] = []
    raw_description = schema.get("description")
    description = _short_description(
        raw_description if isinstance(raw_description, str) else None,
        limit=120,
    )
    if description:
        notes.append(description)
    constraints = (
        ("minimum", ">="),
        ("exclusiveMinimum", ">"),
        ("maximum", "<="),
        ("exclusiveMaximum", "<"),
        ("minLength", "min length"),
        ("maxLength", "max length"),
        ("minItems", "min items"),
        ("maxItems", "max items"),
        ("pattern", "pattern"),
        ("format", "format"),
    )
    for key, label in constraints:
        if key in schema:
            notes.append(f"{label} {schema[key]}")
    if "default" in schema:
        notes.append(f"default {schema['default']!r}")
    return "; ".join(notes) or None


def _build_search_blob(
    public_name: str,
    call: str,
    server: str,
    short_name: str,
    description: str | None,
    input_schema: JsonObject,
) -> str:
    return " ".join([
        public_name,
        call,
        server,
        short_name,
        description or "",
        *_collect_property_names(input_schema),
    ])


def _collect_property_names(schema: JsonSchema) -> list[str]:
    if isinstance(schema, bool):
        return []
    names: list[str] = []
    raw_properties = schema.get("properties")
    properties = raw_properties if isinstance(raw_properties, dict) else {}
    for prop, child in properties.items():
        names.append(prop)
        if isinstance(child, dict):
            names.extend(_collect_property_names(child))
    items = schema.get("items")
    if isinstance(items, dict):
        names.extend(_collect_property_names(items))
    elif isinstance(items, list):
        for item in items:
            if isinstance(item, dict):
                names.extend(_collect_property_names(item))
    return names


def _extract_server_name(tool_name: str, server_names: Iterable[str]) -> str | None:
    for server in sorted(server_names, key=len, reverse=True):
        if tool_name.startswith(f"{server}_"):
            return server
    return None


def _unique_python_aliases(values: Iterable[str]) -> dict[str, str]:
    originals = list(values)
    aliases: dict[str, str] = {}
    used: set[str] = set()
    for original in sorted(originals):
        base = _python_identifier(original)
        alias = base
        if alias in used:
            suffix = hashlib.sha256(original.encode("utf-8")).hexdigest()[:6]
            alias = f"{base}_{suffix}"
        counter = 2
        while alias in used:
            alias = f"{base}_{counter}"
            counter += 1
        aliases[original] = alias
        used.add(alias)
    return aliases


def _python_identifier(value: str) -> str:
    identifier = re.sub(r"[^a-zA-Z0-9_]", "_", value).strip("_").lower()
    identifier = re.sub(r"_+", "_", identifier) or "mcp"
    if identifier[0].isdigit():
        identifier = f"mcp_{identifier}"
    if keyword.iskeyword(identifier):
        identifier = f"{identifier}_"
    return identifier


def _pascal_case(value: str) -> str:
    parts = [part for part in re.split(r"[^a-zA-Z0-9]+", value) if part]
    rendered = "".join(part[:1].upper() + part[1:] for part in parts) or "Anonymous"
    return f"T{rendered}" if rendered[0].isdigit() else rendered


def _valid_identifier(value: str) -> bool:
    return value.isidentifier() and not keyword.iskeyword(value)


def _server_config_fingerprint(name: str, config: JsonObject) -> str:
    payload = json.dumps(
        {"name": name, "config": config},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _schema_fingerprint(schema: JsonSchema) -> str:
    return hashlib.sha256(json.dumps(schema, sort_keys=True, default=str).encode()).hexdigest()


def _resolve_ref(ref: str, root: JsonSchema) -> JsonObject:
    if isinstance(root, bool) or not ref.startswith("#/"):
        return {}
    current: JsonValue = root
    for raw_part in ref[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict):
            return {}
        current = current.get(part, {})
    return current if isinstance(current, dict) else {}


def _merge_all_of(
    schema: JsonObject,
    root: JsonSchema,
) -> JsonObject | None:
    merged_properties: JsonObject = {}
    merged_required: list[JsonValue] = []
    additional_properties: JsonValue = True
    has_additional_properties = False
    raw_members = schema.get("allOf")
    if not isinstance(raw_members, list):
        return None
    for member in raw_members:
        if not isinstance(member, dict):
            return None
        ref = member.get("$ref")
        resolved = _resolve_ref(ref, root) if isinstance(ref, str) else member
        if resolved.get("type") not in {None, "object"}:
            return None
        raw_properties = resolved.get("properties")
        if isinstance(raw_properties, dict):
            merged_properties.update(raw_properties)
        raw_required = resolved.get("required")
        if isinstance(raw_required, list):
            for required in raw_required:
                if isinstance(required, str) and required not in merged_required:
                    merged_required.append(required)
        if "additionalProperties" in resolved:
            additional_properties = resolved["additionalProperties"]
            has_additional_properties = True
    merged: JsonObject = {
        "type": "object",
        "properties": merged_properties,
        "required": merged_required,
    }
    if has_additional_properties:
        merged["additionalProperties"] = additional_properties
    return merged


def _dedupe(blocks: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for block in blocks:
        if block not in seen:
            seen.add(block)
            result.append(block)
    return result
