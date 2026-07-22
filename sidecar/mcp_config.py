from __future__ import annotations

import hashlib
import json
import os
import re
from typing import TYPE_CHECKING, override
from urllib.parse import urlsplit

from fastmcp.client.auth import OAuth
from fastmcp.mcp_config import (
    MCPConfig,
    RemoteMCPServer,
    StdioMCPServer,
    infer_transport_type_from_url,
)
from key_value.aio.stores.filetree import FileTreeStore
from key_value.aio.stores.filetree.store import (
    FileTreeV1CollectionSanitizationStrategy,
    FileTreeV1KeySanitizationStrategy,
)
from pydantic import BaseModel, ConfigDict

from .json_types import JSON_VALUE_ADAPTER, JsonObject, JsonValue
from .models import NormalizedServerInfo, ServerAuth

if TYPE_CHECKING:
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


class NormalizedConfig(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid", strict=True)

    config: MCPConfig
    servers: list[NormalizedServerInfo]


class PersistentCallbackOAuth(OAuth):
    """Reuse the callback registered with a persisted dynamic OAuth client."""

    @override
    async def _initialize(self) -> None:
        await super()._initialize()
        client_info = self.context.client_info
        if client_info is None or not client_info.redirect_uris:
            return
        redirect_uri = client_info.redirect_uris[0]
        parsed = urlsplit(str(redirect_uri))
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parsed.port is None
        ):
            return
        if parsed.path != "/callback" or parsed.query or parsed.fragment:
            return
        self.redirect_port = parsed.port
        self._callback_host = parsed.hostname
        self.context.client_metadata.redirect_uris = [redirect_uri]


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
        auth_kind: ServerAuth | None = (
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
            auth_kind: ServerAuth | None = None
            if raw_auth == "oauth":
                auth = PersistentCallbackOAuth(
                    mcp_url=url,
                    client_name=oauth_client_name,
                    token_storage=oauth_storage,
                    additional_client_metadata={"token_endpoint_auth_method": "none"},
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


def _server_config_fingerprint(name: str, config: JsonObject) -> str:
    payload = json.dumps(
        {"name": name, "config": config},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
