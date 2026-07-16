from __future__ import annotations

import hashlib
import json
import keyword
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, cast

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

PI_ONLY_FIELDS = {"directTools", "lifecycle", "idleTimeout", "disabled"}
REMOTE_TRANSPORTS = {"http", "streamable-http", "sse"}
# A 50-point partial match is generic half-string overlap; require evidence above it.
SEARCH_SCORE_CUTOFF = 51


class NormalizedServerInfo(BaseModel):
    name: str
    transport: str
    config_fingerprint: str
    auth: str | None = None
    description: str | None = None


class SearchMatch(BaseModel):
    name: str
    call: str
    server: str | None = None
    description: str | None = None
    signature: str


class ToolSchemaView(BaseModel):
    name: str
    call: str
    server: str | None = None
    description: str | None = None
    signature: str
    stub: str

    @model_serializer(mode="plain")
    def serialize_compact(self) -> dict[str, Any]:
        values: dict[str, Any] = {
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
    results: list[SearchMatch]


class SchemaResponse(BaseModel):
    tools: list[ToolSchemaView]


class UpstreamStatus(BaseModel):
    name: str
    transport: str
    auth: str | None = None
    tool_count: int = 0


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
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None
    input_type_name: str
    output_type_name: str
    signature: str
    stub: str
    search_blob: str
    input_adapter: TypeAdapter[Any]
    output_adapter: TypeAdapter[Any] | None = None
    wrapped_output_adapter: TypeAdapter[Any] | None = None
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
                prepared.append(
                    (
                        tool,
                        public_name,
                        server,
                        namespace_aliases[server],
                        method_aliases[tool.name],
                    )
                )
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
            builder = StubBuilder(public_name, tool.inputSchema, tool.outputSchema)
            input_type, output_type, tool_definitions = builder.build()
            call = f"{namespace}.{method}"
            signature = f"await {call}(arguments: {input_type}) -> {output_type}"
            output_schema = tool.outputSchema
            wrap_output = bool(output_schema and output_schema.get("x-fastmcp-wrap-result"))
            adapter_schema: dict[str, Any] | bool | None = output_schema
            wrapped_schema: dict[str, Any] | bool | None = None
            if output_schema:
                wrapped_schema = output_schema.get("properties", {}).get("result")
            if wrap_output and output_schema:
                adapter_schema = wrapped_schema or True
            external_name = f"__codemode_{hashlib.sha256(public_name.encode()).hexdigest()[:16]}"

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
                input_schema=tool.inputSchema,
                output_schema=output_schema,
                input_type_name=input_type,
                output_type_name=output_type,
                signature=signature,
                stub="\n\n".join(tool_definitions),
                search_blob=_build_search_blob(
                    public_name,
                    call,
                    server,
                    tool.name,
                    tool.description,
                    tool.inputSchema,
                ),
                input_adapter=TypeAdapter(json_schema_to_type(tool.inputSchema)),
                output_adapter=(
                    TypeAdapter(json_schema_to_type(adapter_schema))
                    if adapter_schema is not None
                    else None
                ),
                wrapped_output_adapter=(
                    TypeAdapter(json_schema_to_type(wrapped_schema))
                    if wrapped_schema is not None
                    else None
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
            facade_calls[(namespace, method)] = public_name

        facade_stubs: list[str] = []
        for namespace in sorted(facade_methods):
            class_name = facade_classes[namespace]
            facade_stubs.append(
                "\n".join([f"class {class_name}:", *facade_methods[namespace]])
            )
            facade_stubs.append(f"{namespace}: {class_name}")

        type_stubs = "\n\n".join(
            [
                "from typing import Any, Literal, NotRequired, TypeAlias, TypedDict",
                *_dedupe(definitions),
                *facade_stubs,
            ]
        )
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
    ) -> list[SearchMatch]:
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
            SearchMatch(
                name=self.tools[name].name,
                call=self.tools[name].call,
                server=self.tools[name].server,
                description=self.tools[name].description,
                signature=self.tools[name].signature,
            )
            for _, _, name in ranked
        ]

    def get_schema(
        self,
        tool_names: Iterable[str],
    ) -> list[ToolSchemaView]:
        requested = list(tool_names)
        unknown = [name for name in requested if name not in self.tools]
        if unknown:
            raise ValueError(f"Unknown tools: {', '.join(unknown)}")
        return [
            ToolSchemaView(
                name=self.tools[name].name,
                call=self.tools[name].call,
                server=self.tools[name].server,
                description=_short_description(self.tools[name].description),
                signature=self.tools[name].signature,
                stub=self.tools[name].stub,
            )
            for name in requested
        ]

    def counts_by_server(self) -> dict[str, int]:
        counts = {server: 0 for server in self.servers}
        for spec in self.tools.values():
            if spec.server:
                counts[spec.server] = counts.get(spec.server, 0) + 1
        return counts

    def validate_arguments(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        spec = self.tools[tool_name]
        value = spec.input_adapter.validate_python(arguments)
        dumped = spec.input_adapter.dump_python(
            value,
            mode="json",
            exclude_unset=True,
        )
        if not isinstance(dumped, dict):
            raise TypeError(f"{tool_name}: arguments did not validate as an object")
        projected = _project_to_input_shape(dumped, arguments)
        if not isinstance(projected, dict):
            raise TypeError(f"{tool_name}: arguments did not normalize as an object")
        return projected

    def normalize_result(self, tool_name: str, result: mcp_types.CallToolResult) -> Any:
        spec = self.tools[tool_name]
        if result.isError:
            message = "Upstream MCP tool returned an error"
            if result.content and isinstance(result.content[0], mcp_types.TextContent):
                message = result.content[0].text
            raise RuntimeError(f"{tool_name}: {message}")

        if spec.output_schema:
            if result.structuredContent is None:
                raise RuntimeError(
                    f"{tool_name}: upstream returned no structuredContent for its declared output schema"
                )
            structured: Any = result.structuredContent
            raw_meta = (result.meta or {}).get("fastmcp")
            wrap_from_meta = isinstance(raw_meta, dict) and bool(raw_meta.get("wrap_result"))
            if spec.output_wrap_result or wrap_from_meta:
                if not isinstance(structured, dict) or "result" not in structured:
                    raise RuntimeError(f"{tool_name}: wrapped output omitted the result field")
                structured = structured["result"]
            adapter = spec.output_adapter
            if wrap_from_meta and not spec.output_wrap_result:
                adapter = spec.wrapped_output_adapter
            if adapter is None:
                return _normalize_json_value(to_jsonable_python(structured))
            validated = adapter.validate_python(structured)
            return _normalize_json_value(adapter.dump_python(validated, mode="json"))

        if result.structuredContent is not None:
            return _normalize_json_value(to_jsonable_python(result.structuredContent))
        if len(result.content) == 1 and isinstance(result.content[0], mcp_types.TextContent):
            return _normalize_text_result(result.content[0].text)
        return [block.model_dump(mode="json", by_alias=True) for block in result.content]


def _project_to_input_shape(normalized: Any, supplied: Any) -> Any:
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


def _normalize_json_value(value: Any) -> Any:
    if isinstance(value, str):
        return _normalize_text_result(value)
    if isinstance(value, dict):
        return {key: _normalize_json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_json_value(item) for item in value]
    return value


def _normalize_text_result(text: str) -> Any:
    stripped = text.strip()
    if stripped in {"null", "true", "false"}:
        return json.loads(stripped)
    if not stripped.startswith(("{", "[")):
        return text
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return text
    return _normalize_json_value(parsed) if isinstance(parsed, (dict, list)) else text


def load_mcp_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"MCP config not found: {path}")
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        raise ValueError(f"MCP config is empty: {path}")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("mcp.json root must be an object")
    return parsed


def normalize_mcp_config(
    raw_config: dict[str, Any],
    *,
    oauth_storage_dir: Path,
    oauth_client_name: str = "pi-mcp-codemode",
) -> NormalizedConfig:
    server_block = raw_config.get("mcpServers", raw_config)
    if not isinstance(server_block, dict):
        raise ValueError("mcp.json must contain an object at the root or under mcpServers")

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
            raise ValueError(f"MCP server {name!r} must be an object")
        if value.get("disabled") is True:
            continue
        cleaned = {key: item for key, item in value.items() if key not in PI_ONLY_FIELDS}
        config_fingerprint = _server_config_fingerprint(name, cleaned)

        if "command" in cleaned:
            normalized = StdioMCPServer.model_validate(
                {**cleaned, "transport": "stdio", "type": "stdio"}
            )
            normalized_servers[name] = normalized
            server_infos.append(
                NormalizedServerInfo(
                    name=name,
                    transport="stdio",
                    config_fingerprint=config_fingerprint,
                    description=normalized.description,
                )
            )
            continue

        if "url" in cleaned:
            transport = cleaned.get("transport") or cleaned.get("type")
            if transport is None:
                transport = infer_transport_type_from_url(cleaned["url"])
            if transport not in REMOTE_TRANSPORTS:
                raise ValueError(f"Unsupported MCP transport for {name}: {transport}")
            raw_auth = cleaned.get("auth")
            auth: Any = raw_auth
            auth_kind: str | None = None
            if raw_auth == "oauth":
                auth = OAuth(
                    mcp_url=cleaned["url"],
                    client_name=oauth_client_name,
                    token_storage=oauth_storage,
                )
                auth_kind = "oauth"
            elif isinstance(raw_auth, str) and raw_auth:
                auth_kind = "bearer"
            normalized = RemoteMCPServer.model_validate(
                {**cleaned, "transport": transport, "auth": auth}
            )
            normalized_servers[name] = normalized
            server_infos.append(
                NormalizedServerInfo(
                    name=name,
                    transport="sse" if transport == "sse" else "http",
                    config_fingerprint=config_fingerprint,
                    auth=auth_kind,
                    description=normalized.description,
                )
            )
            continue

        raise ValueError(f"MCP server {name!r} must define either command or url")

    if not normalized_servers:
        raise ValueError("No enabled MCP servers were found in mcp.json")
    return NormalizedConfig(
        config=MCPConfig(mcpServers=normalized_servers),
        servers=server_infos,
    )


class StubBuilder:
    def __init__(
        self,
        tool_name: str,
        input_schema: dict[str, Any],
        output_schema: dict[str, Any] | None,
    ) -> None:
        self.tool_name = tool_name
        self.input_schema = input_schema
        self.output_schema = output_schema
        self._definitions: list[str] = []
        self._seen: dict[tuple[str, str], str] = {}

    def build(self) -> tuple[str, str, list[str]]:
        input_type = self._ensure_named_type(
            f"{_pascal_case(self.tool_name)}Args",
            self.input_schema,
            self.input_schema,
        )
        output_schema: dict[str, Any] | bool = self.output_schema or True
        if self.output_schema and self.output_schema.get("x-fastmcp-wrap-result"):
            output_schema = self.output_schema.get("properties", {}).get("result", True)
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
        schema: dict[str, Any] | bool,
        root: dict[str, Any] | bool,
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
        schema: dict[str, Any] | bool,
        root: dict[str, Any] | bool,
        name: str,
    ) -> str:
        if isinstance(schema, bool):
            return "Any"
        if "$ref" in schema:
            return self._type_expr(_resolve_ref(schema["$ref"], root), root, name)
        if "const" in schema:
            return f"Literal[{schema['const']!r}]"
        if schema.get("enum"):
            return f"Literal[{', '.join(repr(value) for value in schema['enum'])}]"
        alternatives = schema.get("anyOf") or schema.get("oneOf")
        if alternatives:
            rendered = [
                self._type_expr(member, root, f"{name}Option{index}")
                for index, member in enumerate(alternatives, start=1)
            ]
            return " | ".join(dict.fromkeys(rendered)) or "Any"
        if "allOf" in schema:
            merged = _merge_all_of(schema, root)
            return "Any" if merged is None else self._type_expr(merged, root, name)

        raw_type = schema.get("type")
        if isinstance(raw_type, list):
            rendered = [
                self._type_expr({**schema, "type": member}, root, name)
                for member in raw_type
            ]
            return " | ".join(dict.fromkeys(rendered)) or "Any"
        if raw_type is None:
            if "properties" in schema or "additionalProperties" in schema:
                raw_type = "object"
            elif "items" in schema:
                raw_type = "array"
            else:
                return "Any"

        primitives = {
            "string": "str",
            "integer": "int",
            "number": "float",
            "boolean": "bool",
            "null": "None",
        }
        if raw_type in primitives:
            return primitives[raw_type]
        if raw_type == "array":
            items = schema.get("items", True)
            if isinstance(items, list):
                members = [
                    self._type_expr(
                        cast(dict[str, Any] | bool, item)
                        if isinstance(item, (dict, bool))
                        else True,
                        root,
                        f"{name}Item{index}",
                    )
                    for index, item in enumerate(items, start=1)
                ]
                return f"tuple[{', '.join(members)}]"
            return f"list[{self._type_expr(items, root, f'{name}Item')}]"
        if raw_type == "object":
            properties = schema.get("properties") or {}
            if properties:
                if any(not _valid_identifier(prop) for prop in properties):
                    return "dict[str, Any]"
                key = (name, _schema_fingerprint(schema))
                if key in self._seen:
                    return self._seen[key]
                self._seen[key] = name
                required = set(schema.get("required") or [])
                lines = [f"class {name}(TypedDict):"]
                for prop, prop_schema in properties.items():
                    prop_type = self._type_expr(prop_schema, root, f"{name}{_pascal_case(prop)}")
                    wrapper = prop_type if prop in required else f"NotRequired[{prop_type}]"
                    comment = _field_comment(prop_schema)
                    suffix = f"  # {comment}" if comment else ""
                    lines.append(f"    {prop}: {wrapper}{suffix}")
                self._definitions.append("\n".join(lines))
                return name
            additional = schema.get("additionalProperties", True)
            return f"dict[str, {self._type_expr(additional, root, f'{name}Value')}]"
        return "Any"


def _short_description(description: str | None, limit: int = 240) -> str | None:
    if description is None:
        return None
    first_paragraph = description.split("\n\n", 1)[0]
    compact = " ".join(first_paragraph.split())
    if len(compact) <= limit:
        return compact
    return f"{compact[: limit - 1].rstrip()}…"


def _field_comment(schema: dict[str, Any] | bool) -> str | None:
    if isinstance(schema, bool):
        return None
    notes: list[str] = []
    description = _short_description(schema.get("description"), limit=120)
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
    input_schema: dict[str, Any],
) -> str:
    return " ".join(
        [
            public_name,
            call,
            server,
            short_name,
            description or "",
            *_collect_property_names(input_schema),
        ]
    )


def _collect_property_names(schema: dict[str, Any] | bool) -> list[str]:
    if isinstance(schema, bool):
        return []
    names: list[str] = []
    for prop, child in (schema.get("properties") or {}).items():
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


def _server_config_fingerprint(name: str, config: dict[str, Any]) -> str:
    payload = json.dumps(
        {"name": name, "config": config},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _schema_fingerprint(schema: dict[str, Any] | bool) -> str:
    return hashlib.sha256(json.dumps(schema, sort_keys=True, default=str).encode()).hexdigest()


def _resolve_ref(ref: str, root: dict[str, Any] | bool) -> dict[str, Any]:
    if isinstance(root, bool) or not ref.startswith("#/"):
        return {}
    current: Any = root
    for raw_part in ref[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict):
            return {}
        current = current.get(part, {})
    return current if isinstance(current, dict) else {}


def _merge_all_of(
    schema: dict[str, Any],
    root: dict[str, Any] | bool,
) -> dict[str, Any] | None:
    merged: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
    for member in schema.get("allOf") or []:
        resolved = _resolve_ref(member["$ref"], root) if "$ref" in member else member
        if resolved.get("type") not in {None, "object"}:
            return None
        merged["properties"].update(resolved.get("properties") or {})
        merged["required"] = list(
            dict.fromkeys([*merged["required"], *(resolved.get("required") or [])])
        )
        if "additionalProperties" in resolved:
            merged["additionalProperties"] = resolved["additionalProperties"]
    return merged


def _dedupe(blocks: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for block in blocks:
        if block not in seen:
            seen.add(block)
            result.append(block)
    return result
