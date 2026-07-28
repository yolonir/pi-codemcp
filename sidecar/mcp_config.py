from __future__ import annotations

import hashlib
import json
import os
import re
import time
from contextlib import suppress
from typing import TYPE_CHECKING, Any, override
from urllib.parse import urlsplit

import httpx
from fastmcp.client.auth import OAuth
from fastmcp.client.auth.oauth import TokenStorageAdapter
from fastmcp.mcp_config import (
    MCPConfig,
    RemoteMCPServer,
    StdioMCPServer,
    infer_transport_type_from_url,
)
from key_value.aio.adapters.pydantic import PydanticAdapter
from key_value.aio.stores.filetree import FileTreeStore
from key_value.aio.stores.filetree.store import (
    FileTreeV1CollectionSanitizationStrategy,
    FileTreeV1KeySanitizationStrategy,
)
from mcp.client.auth.utils import (
    build_oauth_authorization_server_metadata_discovery_urls,
    build_protected_resource_metadata_discovery_urls,
    create_oauth_metadata_request,
    handle_auth_metadata_response,
    handle_protected_resource_response,
)
from mcp.shared.auth import OAuthMetadata, ProtectedResourceMetadata
from pydantic import BaseModel, ConfigDict

from .json_types import JSON_VALUE_ADAPTER, JsonObject, JsonValue
from .models import NormalizedServerInfo, ServerAuth

if TYPE_CHECKING:
    from pathlib import Path

    from key_value.aio.protocols import AsyncKeyValue
    from mcp.shared.auth import OAuthClientInformationFull
    from pydantic import AnyUrl

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


class CodemcpTokenStorage(TokenStorageAdapter):
    """Token storage that keeps OAuth state usable across sidecar restarts.

    The upstream adapter evicts the client registration once the server-announced
    client_secret_expires_at passes (Linear announces 24 hours), which silently
    forces a full browser re-login after the next token expiry. A genuinely dead
    secret still surfaces as invalid_client and re-registers, so persisting the
    registration is strictly better. This adapter also persists the discovered
    authorization-server metadata so token refresh hits the real token endpoint
    instead of the SDK's "<origin>/token" fallback (a 404 for e.g. Outline).
    """

    def __init__(self, async_key_value: AsyncKeyValue, server_url: str) -> None:
        super().__init__(async_key_value, server_url)
        self._storage_oauth_metadata = PydanticAdapter[OAuthMetadata](
            default_collection="mcp-oauth-metadata",
            key_value=async_key_value,
            pydantic_model=OAuthMetadata,
            raise_on_validation_error=True,
        )
        self._storage_protected_resource = PydanticAdapter[ProtectedResourceMetadata](
            default_collection="mcp-oauth-protected-resource",
            key_value=async_key_value,
            pydantic_model=ProtectedResourceMetadata,
            raise_on_validation_error=True,
        )

    @override
    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        await self._storage_client_info.put(
            key=self._get_client_info_cache_key(),
            value=client_info,
        )

    @override
    async def clear(self) -> None:
        await super().clear()
        await self._storage_oauth_metadata.delete(key=self._oauth_metadata_cache_key())
        await self._storage_protected_resource.delete(key=self._protected_resource_cache_key())

    async def get_oauth_metadata(self) -> OAuthMetadata | None:
        result: OAuthMetadata | None = await self._storage_oauth_metadata.get(
            key=self._oauth_metadata_cache_key()
        )
        return result

    async def set_oauth_metadata(self, metadata: OAuthMetadata) -> None:
        await self._storage_oauth_metadata.put(
            key=self._oauth_metadata_cache_key(),
            value=metadata,
        )

    async def get_protected_resource_metadata(self) -> ProtectedResourceMetadata | None:
        result: ProtectedResourceMetadata | None = await self._storage_protected_resource.get(
            key=self._protected_resource_cache_key()
        )
        return result

    async def set_protected_resource_metadata(self, metadata: ProtectedResourceMetadata) -> None:
        await self._storage_protected_resource.put(
            key=self._protected_resource_cache_key(),
            value=metadata,
        )

    def _oauth_metadata_cache_key(self) -> str:
        return f"{self._server_url}/oauth_metadata"

    def _protected_resource_cache_key(self) -> str:
        return f"{self._server_url}/protected_resource"


class PersistentCallbackOAuth(OAuth):
    """OAuth provider hardened for long-lived shared file token storage.

    On top of reusing the callback registered with a persisted dynamic client:
    - discovered authorization-server metadata is persisted and restored so token
      refresh works in fresh sidecar processes (the SDK otherwise falls back to
      "<origin>/token", which 404s for servers like Outline and turns every
      access-token expiry into a forced interactive re-login);
    - a refresh response without refresh_token keeps the previous one (RFC 6749
      section 6 allows omission when the refresh token does not rotate);
    - a failed refresh adopts fresher tokens another sidecar process may have
      stored instead of dropping straight into the browser flow.
    """

    def __init__(
        self,
        *,
        mcp_url: str,
        client_name: str,
        token_storage: AsyncKeyValue,
        additional_client_metadata: dict[str, Any] | None = None,
    ) -> None:
        self._codemcp_token_store = token_storage
        self._persisted_oauth_metadata: OAuthMetadata | None = None
        self._persisted_protected_resource: ProtectedResourceMetadata | None = None
        super().__init__(
            mcp_url=mcp_url,
            client_name=client_name,
            token_storage=token_storage,
            additional_client_metadata=additional_client_metadata,
        )

    @override
    def _bind(self, mcp_url: str) -> None:
        super()._bind(mcp_url)
        if isinstance(self.token_storage_adapter, CodemcpTokenStorage):
            return
        storage = CodemcpTokenStorage(self._codemcp_token_store, self.mcp_url)
        self.token_storage_adapter = storage
        self.context.storage = storage

    @override
    async def _initialize(self) -> None:
        await super()._initialize()
        client_info = self.context.client_info
        if client_info is not None and client_info.redirect_uris:
            self._reuse_registered_callback(client_info.redirect_uris[0])
        storage = self.token_storage_adapter
        if not isinstance(storage, CodemcpTokenStorage):
            return
        if self.context.oauth_metadata is None:
            self.context.oauth_metadata = await storage.get_oauth_metadata()
            self._persisted_oauth_metadata = self.context.oauth_metadata
        if self.context.protected_resource_metadata is None:
            self.context.protected_resource_metadata = (
                await storage.get_protected_resource_metadata()
            )
            self._persisted_protected_resource = self.context.protected_resource_metadata
        tokens = self.context.current_tokens
        if tokens is not None and tokens.expires_in and await storage.get_token_expiry() is None:
            # Without the absolute expiry record the upstream fallback re-applies the
            # stale relative expires_in from now; the expired access token then looks
            # valid, gets rejected with a 401, and the flow goes interactive.
            self.context.token_expiry_time = time.time() - 1

    @override
    async def _refresh_token(self) -> httpx.Request:
        if self.context.oauth_metadata is None:
            await self._discover_server_metadata()
        return await super()._refresh_token()

    @override
    async def _handle_token_response(self, response: httpx.Response) -> None:
        await super()._handle_token_response(response)
        await self._persist_discovered_metadata()

    @override
    async def _handle_refresh_response(self, response: httpx.Response) -> bool:
        previous_tokens = self.context.current_tokens
        previous_access_token = previous_tokens.access_token if previous_tokens else None
        previous_refresh_token = previous_tokens.refresh_token if previous_tokens else None
        if await super()._handle_refresh_response(response):
            await self._restore_unrotated_refresh_token(previous_refresh_token)
            await self._persist_discovered_metadata()
            return True
        return await self._adopt_tokens_refreshed_elsewhere(previous_access_token)

    def _reuse_registered_callback(self, redirect_uri: AnyUrl) -> None:
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

    async def _discover_server_metadata(self) -> None:
        """Best-effort OAuth discovery so refresh uses the real token endpoint."""
        with suppress(httpx.HTTPError, ValueError):
            async with self.httpx_client_factory() as client:
                await self._discover_protected_resource(client)
                await self._discover_authorization_server(client)
        await self._persist_discovered_metadata()

    async def _discover_protected_resource(self, client: httpx.AsyncClient) -> None:
        if self.context.protected_resource_metadata is not None:
            return
        for url in build_protected_resource_metadata_discovery_urls(
            None,
            self.context.server_url,
        ):
            response = await client.send(create_oauth_metadata_request(url))
            prm = await handle_protected_resource_response(response)
            if prm is not None:
                self.context.protected_resource_metadata = prm
                self.context.auth_server_url = str(prm.authorization_servers[0])
                return

    async def _discover_authorization_server(self, client: httpx.AsyncClient) -> None:
        if self.context.oauth_metadata is not None:
            return
        for url in build_oauth_authorization_server_metadata_discovery_urls(
            self.context.auth_server_url,
            self.context.server_url,
        ):
            response = await client.send(create_oauth_metadata_request(url))
            ok, metadata = await handle_auth_metadata_response(response)
            if not ok:
                return
            if metadata is not None:
                self.context.oauth_metadata = metadata
                return

    async def _persist_discovered_metadata(self) -> None:
        storage = self.token_storage_adapter
        if not isinstance(storage, CodemcpTokenStorage):
            return
        metadata = self.context.oauth_metadata
        if metadata is not None and metadata != self._persisted_oauth_metadata:
            await storage.set_oauth_metadata(metadata)
            self._persisted_oauth_metadata = metadata
        resource = self.context.protected_resource_metadata
        if resource is not None and resource != self._persisted_protected_resource:
            await storage.set_protected_resource_metadata(resource)
            self._persisted_protected_resource = resource

    async def _restore_unrotated_refresh_token(self, previous_refresh_token: str | None) -> None:
        tokens = self.context.current_tokens
        if tokens is None or tokens.refresh_token is not None or previous_refresh_token is None:
            return
        # RFC 6749 section 6: the server may omit refresh_token when it does not
        # rotate; the SDK overwrites the stored token set and would lose it.
        tokens.refresh_token = previous_refresh_token
        await self.context.storage.set_tokens(tokens)

    async def _adopt_tokens_refreshed_elsewhere(self, previous_access_token: str | None) -> bool:
        storage = self.token_storage_adapter
        if not isinstance(storage, CodemcpTokenStorage):
            return False
        stored = await storage.get_tokens()
        if (
            stored is None
            or not stored.access_token
            or stored.access_token == previous_access_token
        ):
            return False
        expiry = await storage.get_token_expiry()
        if expiry is not None and time.time() > expiry:
            return False
        # Another sidecar process rotated the refresh token first and stored the
        # result; adopt it instead of dropping into the interactive flow.
        self.context.current_tokens = stored
        if expiry is not None:
            self.context.token_expiry_time = expiry
        elif stored.expires_in is not None:
            self.context.token_expiry_time = time.time() + stored.expires_in
        else:
            self.context.token_expiry_time = None
        return True


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
