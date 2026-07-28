from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

import httpx
from key_value.aio.stores.filetree import FileTreeStore
from key_value.aio.stores.filetree.store import (
    FileTreeV1CollectionSanitizationStrategy,
    FileTreeV1KeySanitizationStrategy,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthMetadata, OAuthToken

from sidecar.mcp_config import CodemcpTokenStorage, PersistentCallbackOAuth

if TYPE_CHECKING:
    from pathlib import Path

MCP_URL = "https://example.test/mcp"
CALLBACK_URL = "http://localhost:53187/callback"


def _store(directory: Path) -> FileTreeStore:
    return FileTreeStore(
        data_directory=directory,
        key_sanitization_strategy=FileTreeV1KeySanitizationStrategy(directory),
        collection_sanitization_strategy=FileTreeV1CollectionSanitizationStrategy(
            directory
        ),
    )


def _oauth(directory: Path) -> PersistentCallbackOAuth:
    return PersistentCallbackOAuth(
        mcp_url=MCP_URL,
        client_name="test-client",
        token_storage=_store(directory),
        additional_client_metadata={"token_endpoint_auth_method": "none"},
    )


def _client_info(**overrides: object) -> OAuthClientInformationFull:
    payload: dict[str, object] = {
        "client_id": "client-id",
        "redirect_uris": [CALLBACK_URL],
    }
    payload.update(overrides)
    return OAuthClientInformationFull.model_validate(payload)


def _token_response(status: int, payload: dict[str, object]) -> httpx.Response:
    return httpx.Response(
        status,
        json=payload,
        request=httpx.Request("POST", "https://example.test/oauth/token"),
    )


async def test_client_info_ignores_secret_expiry_ttl(tmp_path: Path) -> None:
    storage = CodemcpTokenStorage(_store(tmp_path), MCP_URL)
    info = _client_info(
        client_secret="secret",
        client_secret_expires_at=int(time.time()) + 60,
    )
    await storage.set_client_info(info)

    stored_files = list(tmp_path.rglob("S_https*client_info*.json"))
    assert len(stored_files) == 1
    raw = json.loads(stored_files[0].read_text(encoding="utf-8"))
    assert raw.get("expires_at") is None
    loaded = await storage.get_client_info()
    assert loaded is not None
    assert loaded.client_id == "client-id"


async def test_refresh_response_keeps_unrotated_refresh_token(tmp_path: Path) -> None:
    oauth = _oauth(tmp_path)
    oauth.context.current_tokens = OAuthToken(
        access_token="old-access",
        refresh_token="keep-me",
        expires_in=1,
    )
    oauth.context.client_info = _client_info()

    response = _token_response(
        200,
        {"access_token": "new-access", "token_type": "Bearer", "expires_in": 3600},
    )
    assert await oauth._handle_refresh_response(response) is True

    tokens: OAuthToken | None = oauth.context.current_tokens
    assert tokens is not None
    assert tokens.access_token == "new-access"
    assert tokens.refresh_token == "keep-me"
    stored = await oauth.context.storage.get_tokens()
    assert stored is not None
    assert stored.refresh_token == "keep-me"


async def test_failed_refresh_adopts_tokens_stored_by_another_process(
    tmp_path: Path,
) -> None:
    oauth = _oauth(tmp_path)
    oauth.context.current_tokens = OAuthToken(
        access_token="stale-access",
        refresh_token="rotated-away",
        expires_in=1,
    )
    oauth.context.client_info = _client_info()

    other = _oauth(tmp_path)
    await other.context.storage.set_tokens(
        OAuthToken(
            access_token="fresh-access", refresh_token="fresh-refresh", expires_in=3600
        )
    )

    response = _token_response(400, {"error": "invalid_grant"})
    assert await oauth._handle_refresh_response(response) is True

    tokens: OAuthToken | None = oauth.context.current_tokens
    assert tokens is not None
    assert tokens.access_token == "fresh-access"
    assert tokens.refresh_token == "fresh-refresh"
    assert oauth.context.is_token_valid()


async def test_failed_refresh_without_newer_tokens_goes_interactive(
    tmp_path: Path,
) -> None:
    oauth = _oauth(tmp_path)
    oauth.context.current_tokens = OAuthToken(
        access_token="stale-access",
        refresh_token="dead",
        expires_in=1,
    )
    oauth.context.client_info = _client_info()

    response = _token_response(400, {"error": "invalid_grant"})
    assert await oauth._handle_refresh_response(response) is False
    tokens: OAuthToken | None = oauth.context.current_tokens
    assert tokens is None


async def test_initialize_restores_persisted_server_metadata(tmp_path: Path) -> None:
    storage = CodemcpTokenStorage(_store(tmp_path), MCP_URL)
    await storage.set_oauth_metadata(
        OAuthMetadata.model_validate(
            {
                "issuer": "https://example.test",
                "authorization_endpoint": "https://example.test/oauth/authorize",
                "token_endpoint": "https://example.test/oauth/token",
            }
        )
    )

    oauth = _oauth(tmp_path)
    await oauth._initialize()

    metadata = oauth.context.oauth_metadata
    assert metadata is not None
    assert str(metadata.token_endpoint).endswith("/oauth/token")


async def test_initialize_treats_missing_expiry_record_as_expired(
    tmp_path: Path,
) -> None:
    oauth = _oauth(tmp_path)
    await oauth.context.storage.set_tokens(
        OAuthToken(access_token="access", refresh_token="refresh", expires_in=3600)
    )
    storage = oauth.token_storage_adapter
    assert isinstance(storage, CodemcpTokenStorage)
    await storage._key_value_store.delete(
        key=f"{MCP_URL}/token_expiry",
        collection="mcp-oauth-token-expiry",
    )

    fresh = _oauth(tmp_path)
    await fresh._initialize()

    assert fresh.context.current_tokens is not None
    assert not fresh.context.is_token_valid()


async def test_refresh_discovers_token_endpoint_when_metadata_missing(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/oauth-authorization-server":
            return httpx.Response(
                200,
                json={
                    "issuer": "https://example.test",
                    "authorization_endpoint": "https://example.test/oauth/authorize",
                    "token_endpoint": "https://example.test/oauth/token",
                },
            )
        return httpx.Response(404)

    def factory(
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    oauth = _oauth(tmp_path)
    oauth.httpx_client_factory = factory
    oauth.context.current_tokens = OAuthToken(
        access_token="access",
        refresh_token="refresh",
        expires_in=1,
    )
    oauth.context.client_info = _client_info()

    request = await oauth._refresh_token()

    assert str(request.url) == "https://example.test/oauth/token"
    persisted = await CodemcpTokenStorage(
        _store(tmp_path), MCP_URL
    ).get_oauth_metadata()
    assert persisted is not None
    assert str(persisted.token_endpoint) == "https://example.test/oauth/token"
