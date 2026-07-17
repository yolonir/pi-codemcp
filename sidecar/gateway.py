from __future__ import annotations

import ast
import asyncio
import os
import textwrap
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from fastmcp import Client, FastMCP
from fastmcp.mcp_config import RemoteMCPServer, StdioMCPServer

from .catalog_cache import CatalogCache
from .executor import ExecutionResponse, MontyExecutor
from .schemas import (
    NormalizedConfig,
    NormalizedServerInfo,
    SearchResponse,
    ServerToolSummary,
    StatusResponse,
    ToolCatalog,
    UpstreamStatus,
    UpstreamToolStatus,
    load_mcp_json,
    normalize_mcp_config,
)
from .settings import CodeMcpSettings, load_settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable

    from fastmcp.client.transports import ClientTransport
    from mcp import types as mcp_types

    from .json_types import JsonObject, JsonValue

DEFAULT_AGENT_DIR = Path.home() / ".pi" / "agent"
CODEMCP_AGENT_DIR_ENV = "PI_CODEMCP_AGENT_DIR"
PI_AGENT_DIR_ENV = "PI_CODING_AGENT_DIR"
type ServerConfig = StdioMCPServer | RemoteMCPServer


@dataclass(slots=True)
class ServerHandle:
    info: NormalizedServerInfo
    server_config: ServerConfig
    cache: CatalogCache
    tools: list[mcp_types.Tool] | None = None
    client: Client[ClientTransport] | None = None
    _exit_stack: AsyncExitStack | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @classmethod
    def create(
        cls,
        info: NormalizedServerInfo,
        server_config: ServerConfig,
        cache: CatalogCache,
    ) -> ServerHandle:
        return cls(
            info=info,
            server_config=server_config,
            cache=cache,
            tools=cache.load(info.name, info.config_fingerprint),
        )

    async def discover(self, *, force: bool = False) -> list[mcp_types.Tool]:
        async with self._lock:
            if self.tools is not None and not force:
                return self.tools
            was_connected = self.client is not None
            client = await self._connect_locked()
            try:
                tools = await client.list_tools()
                self.tools = tools
                await asyncio.to_thread(
                    self.cache.save,
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
        arguments: JsonObject,
        *,
        timeout_seconds: float,
    ) -> mcp_types.CallToolResult:
        async with self._lock:
            client = await self._connect_locked()
        return await client.call_tool_mcp(name, arguments, timeout=timeout_seconds)

    async def close(self) -> None:
        async with self._lock:
            await self._disconnect_locked()

    async def _connect_locked(self) -> Client[ClientTransport]:
        if self.client is not None:
            return self.client
        exit_stack = AsyncExitStack()
        try:
            client = await exit_stack.enter_async_context(
                Client(
                    self.server_config.to_transport(),
                    name=f"pi-codemcp-{self.info.name}",
                )
            )
        except BaseException:
            await exit_stack.aclose()
            raise
        self._exit_stack = exit_stack
        self.client = client
        return client

    async def _disconnect_locked(self) -> None:
        exit_stack, self._exit_stack = self._exit_stack, None
        self.client = None
        if exit_stack is not None:
            await exit_stack.aclose()


@dataclass(slots=True)
class GatewayRuntime:
    config_path: Path
    settings_path: Path
    settings: CodeMcpSettings
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
        settings_path: Path | None = None,
    ) -> GatewayRuntime:
        resolved_settings_path = settings_path or catalog_cache_dir.parent / "settings.json"
        settings = load_settings(resolved_settings_path)
        raw = load_mcp_json(config_path)
        normalized = normalize_mcp_config(
            raw,
            oauth_storage_dir=oauth_storage_dir,
        )
        cache = CatalogCache(
            catalog_cache_dir,
            max_age_seconds=settings.cache_ttl_seconds,
        )
        info_by_name = {server.name: server for server in normalized.servers}
        handles = {
            name: ServerHandle.create(info_by_name[name], server_config, cache)
            for name, server_config in normalized.config.mcpServers.items()
        }
        catalog = ToolCatalog.from_server_tools(
            {
                name: [
                    tool for tool in handle.tools or [] if settings.tool_enabled(name, tool.name)
                ]
                for name, handle in handles.items()
            },
            handles.keys(),
        )
        return cls(
            config_path=config_path,
            settings_path=resolved_settings_path,
            settings=settings,
            normalized=normalized,
            handles=handles,
            catalog=catalog,
            executor=MontyExecutor(catalog, settings=settings.execution_settings()),
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

    async def execute(self, code: str) -> ExecutionResponse:
        await self._ensure_servers_discovered(self._referenced_servers(code))
        self.executor.update_catalog(self.catalog)

        async def dispatch(public_name: str, arguments: JsonObject) -> JsonValue:
            spec = self.catalog.tools[public_name]
            handle = self.handles[spec.server]
            result = await handle.call_tool(
                spec.backend_name,
                arguments,
                timeout_seconds=self.executor.settings.tool_timeout_seconds,
            )
            return self.catalog.normalize_result(public_name, result)

        return await self.executor.execute(code, dispatch)

    async def discover(self, server: str) -> StatusResponse:
        handle = self.handles.get(server)
        if handle is None:
            raise ValueError(f"Unknown or disabled MCP server: {server}")
        await handle.discover(force=True)
        await self._rebuild_catalog()
        return self.status()

    async def reload_settings(self) -> StatusResponse:
        self.settings = load_settings(self.settings_path)
        for handle in self.handles.values():
            handle.cache.max_age_seconds = self.settings.cache_ttl_seconds
        self.executor.settings = self.settings.execution_settings()
        await self._rebuild_catalog()
        return self.status()

    def status(self) -> StatusResponse:
        upstreams: list[UpstreamStatus] = []
        for server in self.normalized.servers:
            handle = self.handles.get(server.name)
            all_tools = handle.tools if handle is not None and handle.tools is not None else []
            tools = [
                UpstreamToolStatus(
                    name=tool.name,
                    enabled=self.settings.tool_enabled(server.name, tool.name),
                    description=_compact_description(tool.description),
                )
                for tool in sorted(all_tools, key=lambda item: item.name)
            ]
            upstreams.append(
                UpstreamStatus(
                    name=server.name,
                    transport=server.transport,
                    enabled=server.enabled,
                    connected=handle is not None and handle.client is not None,
                    discovered=handle is not None and handle.tools is not None,
                    auth=server.auth,
                    tool_count=sum(tool.enabled for tool in tools),
                    total_tool_count=len(tools),
                    tools=tools,
                )
            )
        return StatusResponse(
            connected=True,
            config_path=str(self.config_path),
            tool_count=len(self.catalog.tools),
            upstreams=upstreams,
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
        await self._rebuild_catalog()

    async def _rebuild_catalog(self) -> None:
        async with self._catalog_lock:
            self.catalog = ToolCatalog.from_server_tools(
                {
                    name: [
                        tool
                        for tool in handle.tools or []
                        if self.settings.tool_enabled(name, tool.name)
                    ]
                    for name, handle in self.handles.items()
                },
                self.handles.keys(),
            )
            self.executor.update_catalog(self.catalog)

    def _referenced_servers(self, code: str) -> set[str]:
        normalized = textwrap.dedent(code).strip("\n")
        wrapped = f"async def __codemcp_main():\n{textwrap.indent(normalized, '    ')}\n"
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


def _compact_description(description: str | None, limit: int = 160) -> str | None:
    if description is None:
        return None
    compact = " ".join(description.split("\n\n", 1)[0].split())
    return compact if len(compact) <= limit else f"{compact[: limit - 1].rstrip()}…"


@dataclass(slots=True)
class RuntimeState:
    runtime: GatewayRuntime | None = None


_runtime_state = RuntimeState()


def _require_runtime() -> GatewayRuntime:
    if _runtime_state.runtime is None:
        raise RuntimeError("Code Mode sidecar is not initialized")
    return _runtime_state.runtime


def _runtime_paths() -> tuple[Path, Path, Path, Path]:
    raw_agent_dir = os.environ.get(CODEMCP_AGENT_DIR_ENV) or os.environ.get(PI_AGENT_DIR_ENV)
    agent_dir = Path(raw_agent_dir).expanduser() if raw_agent_dir else DEFAULT_AGENT_DIR
    state_dir = agent_dir / "pi-codemcp"
    return (
        agent_dir / "mcp.json",
        state_dir / "oauth",
        state_dir / "catalog",
        state_dir / "settings.json",
    )


@asynccontextmanager
async def lifespan(_: FastMCP[None]) -> AsyncIterator[None]:
    config_path, oauth_dir, catalog_dir, settings_path = _runtime_paths()
    _runtime_state.runtime = GatewayRuntime.create(
        config_path,
        oauth_dir,
        catalog_dir,
        settings_path,
    )
    try:
        yield
    finally:
        runtime, _runtime_state.runtime = _runtime_state.runtime, None
        if runtime is not None:
            await runtime.close()


mcp = FastMCP(
    "pi-codemcp-sidecar",
    instructions=(
        "Search for MCP tools and their typed SDK stubs, then execute a sandboxed Python chain."
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
async def discover(server: str) -> StatusResponse:
    """Force-refresh one enabled upstream tool catalog."""
    return await _require_runtime().discover(server)


@mcp.tool
async def reload_settings() -> StatusResponse:
    """Reload persisted CodeMCP settings and tool policy."""
    return await _require_runtime().reload_settings()


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
