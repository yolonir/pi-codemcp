from __future__ import annotations

import ast
import asyncio
import os
import textwrap
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Iterable

from fastmcp import Client, FastMCP
from mcp import types as mcp_types

from .catalog_cache import CatalogCache
from .executor import ExecutionResponse, ExecutionSettings, MontyExecutor
from .schemas import (
    NormalizedConfig,
    NormalizedServerInfo,
    SchemaResponse,
    SearchResponse,
    ServerToolSummary,
    StatusResponse,
    ToolCatalog,
    UpstreamStatus,
    load_mcp_json,
    normalize_mcp_config,
)

DEFAULT_CONFIG_PATH = Path.home() / ".pi" / "agent" / "mcp.json"
DEFAULT_STATE_DIR = Path.home() / ".pi" / "agent" / "pi-mcp-codemode"
DEFAULT_OAUTH_DIR = DEFAULT_STATE_DIR / "oauth"
DEFAULT_CATALOG_DIR = DEFAULT_STATE_DIR / "catalog"


@dataclass(slots=True)
class ServerHandle:
    info: NormalizedServerInfo
    server_config: Any
    cache: CatalogCache
    tools: list[mcp_types.Tool] | None = None
    client: Client[Any] | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @classmethod
    def create(
        cls,
        info: NormalizedServerInfo,
        server_config: Any,
        cache: CatalogCache,
    ) -> ServerHandle:
        return cls(
            info=info,
            server_config=server_config,
            cache=cache,
            tools=cache.load(info.name, info.config_fingerprint),
        )

    async def discover(self) -> list[mcp_types.Tool]:
        async with self._lock:
            if self.tools is not None:
                return self.tools
            was_connected = self.client is not None
            client = await self._connect_locked()
            try:
                tools = await client.list_tools()
                self.tools = tools
                self.cache.save(
                    self.info.name,
                    self.info.config_fingerprint,
                    tools,
                )
                return tools
            finally:
                if not was_connected:
                    await self._disconnect_locked()

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        timeout: float,
    ) -> mcp_types.CallToolResult:
        async with self._lock:
            client = await self._connect_locked()
        return await client.call_tool_mcp(name, arguments, timeout=timeout)

    async def close(self) -> None:
        async with self._lock:
            await self._disconnect_locked()

    async def _connect_locked(self) -> Client[Any]:
        if self.client is not None:
            return self.client
        client: Client[Any] = Client(
            self.server_config.to_transport(),
            name=f"pi-mcp-codemode-{self.info.name}",
        )
        await client.__aenter__()
        self.client = client
        return client

    async def _disconnect_locked(self) -> None:
        client, self.client = self.client, None
        if client is not None:
            await client.__aexit__(None, None, None)


@dataclass(slots=True)
class GatewayRuntime:
    config_path: Path
    normalized: NormalizedConfig
    handles: dict[str, ServerHandle]
    catalog: ToolCatalog
    executor: MontyExecutor
    _catalog_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @classmethod
    def create(
        cls,
        config_path: Path,
        oauth_storage_dir: Path,
        catalog_cache_dir: Path,
        *,
        settings: ExecutionSettings | None = None,
    ) -> GatewayRuntime:
        raw = load_mcp_json(config_path)
        normalized = normalize_mcp_config(
            raw,
            oauth_storage_dir=oauth_storage_dir,
        )
        cache = CatalogCache(catalog_cache_dir)
        info_by_name = {server.name: server for server in normalized.servers}
        handles = {
            name: ServerHandle.create(info_by_name[name], server_config, cache)
            for name, server_config in normalized.config.mcpServers.items()
        }
        catalog = ToolCatalog.from_server_tools(
            {
                name: handle.tools or []
                for name, handle in handles.items()
            },
            handles.keys(),
        )
        return cls(
            config_path=config_path,
            normalized=normalized,
            handles=handles,
            catalog=catalog,
            executor=MontyExecutor(catalog, settings=settings),
        )

    async def close(self) -> None:
        await asyncio.gather(
            *(handle.close() for handle in self.handles.values()),
            return_exceptions=True,
        )

    async def search(
        self,
        query: str,
        limit: int = 5,
        server: str | None = None,
    ) -> SearchResponse:
        clean_query = query.strip()
        if not clean_query:
            raise ValueError("query must not be empty")
        await self._ensure_catalog_complete()
        bounded_limit = min(max(limit, 1), 20)
        counts = self.catalog.counts_by_server()
        return SearchResponse(
            total_tool_count=len(self.catalog.tools),
            servers=[
                ServerToolSummary(name=server.name, tool_count=counts[server.name])
                for server in self.normalized.servers
                if counts.get(server.name, 0) > 0
            ],
            results=self.catalog.search(clean_query, bounded_limit, server=server),
        )

    async def get_schema(
        self,
        tools: list[str],
    ) -> SchemaResponse:
        if not tools:
            raise ValueError("tools must contain at least one tool name")
        if len(tools) > 20:
            raise ValueError("at most 20 tool schemas can be requested at once")
        requested_servers: set[str] = set()
        for tool_name in tools:
            for server in sorted(self.handles, key=len, reverse=True):
                if tool_name.startswith(f"{server}_"):
                    requested_servers.add(server)
                    break
        await self._ensure_servers_discovered(requested_servers)
        return SchemaResponse(tools=self.catalog.get_schema(tools))

    async def execute(self, code: str) -> ExecutionResponse:
        await self._ensure_servers_discovered(self._referenced_servers(code))
        self.executor.update_catalog(self.catalog)

        async def dispatch(public_name: str, arguments: dict[str, Any]) -> Any:
            spec = self.catalog.tools[public_name]
            handle = self.handles[spec.server]
            result = await handle.call_tool(
                spec.backend_name,
                arguments,
                timeout=self.executor.settings.tool_timeout_seconds,
            )
            return self.catalog.normalize_result(public_name, result)

        return await self.executor.execute(code, dispatch)

    def status(self) -> StatusResponse:
        counts = self.catalog.counts_by_server()
        return StatusResponse(
            connected=True,
            config_path=str(self.config_path),
            tool_count=len(self.catalog.tools),
            upstreams=[
                UpstreamStatus(
                    name=server.name,
                    transport=server.transport,
                    auth=server.auth,
                    tool_count=counts.get(server.name, 0),
                )
                for server in self.normalized.servers
            ],
        )

    async def _ensure_catalog_complete(self) -> None:
        await self._ensure_servers_discovered(self.handles.keys())

    async def _ensure_servers_discovered(self, server_names: Iterable[str]) -> None:
        requested = set(server_names)
        missing = [
            handle
            for name, handle in self.handles.items()
            if name in requested and handle.tools is None
        ]
        if missing:
            await asyncio.gather(*(handle.discover() for handle in missing))
        if not missing:
            return
        async with self._catalog_lock:
            self.catalog = ToolCatalog.from_server_tools(
                {
                    name: handle.tools or []
                    for name, handle in self.handles.items()
                },
                self.handles.keys(),
            )
            self.executor.update_catalog(self.catalog)

    def _referenced_servers(self, code: str) -> set[str]:
        normalized = textwrap.dedent(code).strip("\n")
        wrapped = (
            "async def __codemode_main():\n"
            f"{textwrap.indent(normalized, '    ')}\n"
        )
        try:
            tree = ast.parse(wrapped, mode="exec")
        except SyntaxError:
            return set()
        aliases = {alias: server for server, alias in self.catalog.server_aliases.items()}
        return {
            aliases[node.value.id]
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in aliases
        }


_runtime: GatewayRuntime | None = None


def _require_runtime() -> GatewayRuntime:
    if _runtime is None:
        raise RuntimeError("Code Mode sidecar is not initialized")
    return _runtime


@asynccontextmanager
async def lifespan(_: FastMCP[Any]) -> AsyncIterator[None]:
    global _runtime
    config_path = Path(os.environ.get("PI_MCP_CODEMODE_CONFIG", DEFAULT_CONFIG_PATH)).expanduser()
    oauth_dir = Path(os.environ.get("PI_MCP_CODEMODE_OAUTH_DIR", DEFAULT_OAUTH_DIR)).expanduser()
    catalog_dir = Path(
        os.environ.get("PI_MCP_CODEMODE_CATALOG_DIR", DEFAULT_CATALOG_DIR)
    ).expanduser()
    _runtime = GatewayRuntime.create(config_path, oauth_dir, catalog_dir)
    try:
        yield
    finally:
        runtime, _runtime = _runtime, None
        if runtime is not None:
            await runtime.close()


mcp = FastMCP(
    "pi-mcp-codemode-sidecar",
    instructions=(
        "Search for MCP tools, inspect their compact typed SDK signatures, then execute "
        "a sandboxed Python chain."
    ),
    lifespan=lifespan,
)


@mcp.tool
async def search(
    query: str,
    limit: int = 5,
    server: str | None = None,
) -> SearchResponse:
    """Search configured upstream MCP tools by capability."""
    return await _require_runtime().search(query, limit, server)


@mcp.tool
async def get_schema(tools: list[str]) -> SchemaResponse:
    """Return compact typed SDK signatures for selected upstream tools."""
    return await _require_runtime().get_schema(tools)


@mcp.tool
async def execute(code: str) -> ExecutionResponse:
    """Type-check and run one sandboxed Python MCP SDK chain."""
    return await _require_runtime().execute(code)


@mcp.tool
def status() -> StatusResponse:
    """Report cached catalog and upstream connection state without connecting upstreams."""
    return _require_runtime().status()


def main() -> None:
    mcp.run(transport="stdio", show_banner=False, log_level="ERROR")


if __name__ == "__main__":
    main()
