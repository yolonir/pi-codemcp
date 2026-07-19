from __future__ import annotations

import ast
import asyncio
import textwrap
import time
from contextlib import AsyncExitStack, asynccontextmanager
from typing import TYPE_CHECKING, Literal, NamedTuple, Protocol

import pydantic_monty
from fastmcp import Client, FastMCP
from fastmcp.mcp_config import RemoteMCPServer, StdioMCPServer
from pydantic import BaseModel, ConfigDict

from . import json_types
from .catalog_cache import CatalogCache
from .chains import (
    ChainDependency,
    ChainEnabledChange,
    ChainListResponse,
    ChainScope,
    ChainStatusView,
    ChainStore,
    SaveChainResponse,
    SavedChainManifest,
    ScopedChainStore,
)
from .executor import ExecutionContext, ExecutionResponse, MontyExecutor
from .mcp_config import NormalizedConfig, load_mcp_json, normalize_mcp_config
from .models import (
    NormalizedServerInfo,
    SearchResponse,
    ServerToolSummary,
    StatusResponse,
    UpstreamStatus,
    UpstreamToolStatus,
)
from .runtime_paths import resolve_runtime_paths
from .settings import CodeMcpSettings, load_settings
from .tool_catalog import ToolCatalog

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
    from pathlib import Path

    from fastmcp.client.transports import ClientTransport
    from mcp import types as mcp_types

    from .runtime_paths import RuntimePaths
type ServerConfig = StdioMCPServer | RemoteMCPServer
type JsonObject = json_types.JsonObject
type JsonValue = json_types.JsonValue


class ManagerApplyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    status: StatusResponse
    chains: list[ChainStatusView]


class ServerHandle:
    def __init__(
        self,
        info: NormalizedServerInfo,
        server_config: ServerConfig,
        cache: CatalogCache,
    ) -> None:
        self.info = info
        self.server_config = server_config
        self.cache = cache
        self._tools: list[mcp_types.Tool] | None = None
        self._client: Client[ClientTransport] | None = None
        self._exit_stack: AsyncExitStack | None = None
        self._lock = asyncio.Lock()

    @property
    def tools(self) -> list[mcp_types.Tool] | None:
        return self._tools

    @property
    def client(self) -> Client[ClientTransport] | None:
        return self._client

    @classmethod
    def create(
        cls,
        info: NormalizedServerInfo,
        server_config: ServerConfig,
        cache: CatalogCache,
    ) -> ServerHandle:
        handle = cls(
            info=info,
            server_config=server_config,
            cache=cache,
        )
        handle._tools = cache.load(info.name, info.config_fingerprint)
        return handle

    async def discover(self, *, force: bool = False) -> list[mcp_types.Tool]:
        async with self._lock:
            if self._tools is not None and not force:
                return self._tools
            was_connected = self._client is not None
            client = await self._connect_locked()
            try:
                tools = await client.list_tools()
                self._tools = tools
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
        if self._client is not None:
            return self._client
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
        self._client = client
        return client

    async def _disconnect_locked(self) -> None:
        exit_stack, self._exit_stack = self._exit_stack, None
        self._client = None
        if exit_stack is not None:
            await exit_stack.aclose()


class SaveChainHandler(Protocol):
    async def __call__(
        self,
        *,
        scope: ChainScope,
        name: str,
        description: str,
        code: str,
        input_schema: JsonObject,
        output_schema: JsonObject,
    ) -> SaveChainResponse: ...


class SavedChainHandlers(NamedTuple):
    execute: Callable[[str, JsonObject], Awaitable[ExecutionResponse]]
    save: SaveChainHandler
    list: Callable[[], ChainListResponse]
    revalidate: Callable[[str, ChainScope], Awaitable[ChainStatusView]]
    delete: Callable[[str, ChainScope], Awaitable[ChainListResponse]]


class SavedChainRuntime:
    def __init__(self, handlers: SavedChainHandlers) -> None:
        self.handlers = handlers

    async def execute(self, name: str, arguments: JsonObject) -> ExecutionResponse:
        return await self.handlers.execute(name, arguments)

    async def save(
        self,
        *,
        scope: ChainScope,
        name: str,
        description: str,
        code: str,
        input_schema: JsonObject,
        output_schema: JsonObject,
    ) -> SaveChainResponse:
        return await self.handlers.save(
            scope=scope,
            name=name,
            description=description,
            code=code,
            input_schema=input_schema,
            output_schema=output_schema,
        )

    def list(self) -> ChainListResponse:
        return self.handlers.list()

    async def revalidate(self, name: str, scope: ChainScope) -> ChainStatusView:
        return await self.handlers.revalidate(name, scope)

    async def delete(self, name: str, scope: ChainScope) -> ChainListResponse:
        return await self.handlers.delete(name, scope)


class GatewayRuntime:
    def __init__(
        self,
        *,
        config_path: Path,
        settings_path: Path,
        settings: CodeMcpSettings,
        normalized: NormalizedConfig,
        handles: dict[str, ServerHandle],
        chain_store: ScopedChainStore,
        catalog: ToolCatalog,
        executor: MontyExecutor,
    ) -> None:
        self.config_path = config_path
        self.settings_path = settings_path
        self.settings = settings
        self.normalized = normalized
        self.handles = handles
        self.chain_store = chain_store
        self.catalog = catalog
        self.executor = executor
        self.chains = SavedChainRuntime(
            SavedChainHandlers(
                execute=self._execute_chain,
                save=self._save_chain,
                list=self._list_chains,
                revalidate=self._revalidate_chain,
                delete=self._delete_chain,
            )
        )
        self._catalog_lock = asyncio.Lock()

    @classmethod
    def create(
        cls,
        config_path: Path,
        oauth_storage_dir: Path,
        catalog_cache_dir: Path,
        settings_path: Path | None = None,
        global_chain_dir: Path | None = None,
        project_chain_dir: Path | None = None,
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
        chain_store = ScopedChainStore(
            global_chain_dir or catalog_cache_dir.parent / "chains",
            project_chain_dir,
        )
        catalog = ToolCatalog.from_server_tools(
            {
                name: [
                    tool for tool in handle.tools or [] if settings.tool_enabled(name, tool.name)
                ]
                for name, handle in handles.items()
            },
            handles.keys(),
            chain_store.enabled(),
        )
        return cls(
            config_path=config_path,
            settings_path=resolved_settings_path,
            settings=settings,
            normalized=normalized,
            handles=handles,
            chain_store=chain_store,
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
        servers = [
            ServerToolSummary(name=server.name, tool_count=counts[server.name])
            for server in self.normalized.servers
            if counts.get(server.name, 0) > 0
        ]
        if counts.get("chains", 0) > 0:
            servers.append(ServerToolSummary(name="chains", tool_count=counts["chains"]))
        return SearchResponse(
            total_tool_count=len(self.catalog.tools),
            servers=servers,
            results=self.catalog.search(clean_query, bounded_limit, server=server),
        )

    async def execute(self, code: str) -> ExecutionResponse:
        await self._ensure_servers_discovered(self._required_servers_for_code(code))
        self.executor.update_catalog(self.catalog)
        return await self.executor.execute_graph(code, self._dispatch)

    async def _execute_chain(self, name: str, arguments: JsonObject) -> ExecutionResponse:
        chain = self.chain_store.get(name).chain
        if not chain.enabled:
            return ExecutionResponse(
                ok=False,
                failure_stage="preflight",
                error=f"Saved chain is disabled: {name}",
            )
        await self._ensure_servers_discovered(self._required_servers_for_chain(chain))
        self.executor.update_catalog(self.catalog)
        return await self.executor.execute_saved_chain(chain, arguments, self._dispatch)

    async def _save_chain(
        self,
        *,
        scope: ChainScope,
        name: str,
        description: str,
        code: str,
        input_schema: JsonObject,
        output_schema: JsonObject,
    ) -> SaveChainResponse:
        previous = (
            self.chain_store.get(name, scope).chain
            if self.chain_store.contains(scope, name)
            else None
        )
        candidate = ChainStore.build(
            name=name,
            description=description,
            code=code,
            input_schema=input_schema,
            output_schema=output_schema,
            dependencies=[],
            previous=previous,
        ).model_copy(update={"enabled": True})
        await self._ensure_servers_discovered(self._referenced_servers(code))
        chains = [chain for chain in self.chain_store.enabled() if chain.name != name]
        chains.append(candidate)
        candidate_catalog = self._build_catalog(chains)
        spec = candidate_catalog.tools[candidate.public_name]
        try:
            await self.executor.validate_saved_chain(code, candidate_catalog, spec)
        except (pydantic_monty.MontyTypingError, pydantic_monty.MontySyntaxError) as error:
            if isinstance(error, pydantic_monty.MontyTypingError):
                message = error.display("concise", color=False).strip()
            else:
                message = error.display("type-msg").strip()
            raise ValueError(f"Saved chain failed preflight: {message}") from error
        dependencies = self._chain_dependencies(code, candidate_catalog)
        saved = ChainStore.build(
            name=name,
            description=description,
            code=code,
            input_schema=input_schema,
            output_schema=output_schema,
            dependencies=dependencies,
            previous=previous,
        ).model_copy(update={"enabled": True})
        self.chain_store.save(scope, saved)
        await self._rebuild_catalog()
        return SaveChainResponse(
            chain=self._chain_view(saved.name, scope),
            created=previous is None,
        )

    def _list_chains(self) -> ChainListResponse:
        return ChainListResponse(chains=self._chain_views())

    async def _revalidate_chain(self, name: str, scope: ChainScope) -> ChainStatusView:
        current = self.chain_store.get(name, scope).chain
        await self._ensure_servers_discovered(self._referenced_servers(current.code))
        chains = [chain for chain in self.chain_store.enabled() if chain.name != name]
        chains.append(current)
        candidate_catalog = self._build_catalog(chains)
        spec = candidate_catalog.tools[current.public_name]
        try:
            await self.executor.validate_saved_chain(current.code, candidate_catalog, spec)
        except (pydantic_monty.MontyTypingError, pydantic_monty.MontySyntaxError) as error:
            if isinstance(error, pydantic_monty.MontyTypingError):
                message = error.display("concise", color=False).strip()
            else:
                message = error.display("type-msg").strip()
            raise ValueError(f"Saved chain failed preflight: {message}") from error
        updated = current.model_copy(
            update={
                "dependencies": self._chain_dependencies(current.code, candidate_catalog),
                "validated_at": time.time(),
            }
        )
        self.chain_store.save(scope, updated)
        await self._rebuild_catalog()
        return self._chain_view(name, scope)

    async def _delete_chain(self, name: str, scope: ChainScope) -> ChainListResponse:
        effective = self.chain_store.get(name)
        called_by = self._called_by().get(name, []) if effective.scope == scope else []
        if called_by:
            raise ValueError(
                f"Cannot delete saved chain {name}; it is used by: {', '.join(called_by)}"
            )
        self.chain_store.delete(scope, name)
        await self._rebuild_catalog()
        return self._list_chains()

    async def _dispatch(
        self,
        public_name: str,
        arguments: JsonObject,
        context: ExecutionContext,
    ) -> JsonValue:
        spec = context.catalog.tools[public_name]
        if spec.kind == "saved_chain":
            chain = self.chain_store.get(spec.backend_name).chain
            return await self.executor.execute_nested_chain(chain, arguments, context)

        handle = self.handles[spec.server]
        result = await handle.call_tool(
            spec.backend_name,
            arguments,
            timeout_seconds=min(
                self.executor.settings.tool_timeout_seconds,
                max(0.001, context.remaining_seconds()),
            ),
        )
        return context.catalog.normalize_result(public_name, result)

    async def discover(self, server: str) -> StatusResponse:
        handle = self.handles.get(server)
        if handle is None:
            raise ValueError(f"Unknown or disabled MCP server: {server}")
        await handle.discover(force=True)
        await self._rebuild_catalog()
        return self.status()

    async def reload_settings(self) -> StatusResponse:
        self._load_settings()
        await self._rebuild_catalog()
        return self.status()

    async def apply_manager_changes(
        self,
        changes: list[ChainEnabledChange],
    ) -> ManagerApplyResponse:
        previous_settings = self.settings
        try:
            with self.chain_store.enabled_transaction(changes):
                self._load_settings()
                await self._rebuild_catalog()
        except BaseException:
            self.settings = previous_settings
            for handle in self.handles.values():
                handle.cache.max_age_seconds = previous_settings.cache_ttl_seconds
            self.executor.settings = previous_settings.execution_settings()
            await self._rebuild_catalog()
            raise
        return ManagerApplyResponse(
            status=self.status(),
            chains=self._chain_views(),
        )

    def _load_settings(self) -> None:
        self.settings = load_settings(self.settings_path)
        for handle in self.handles.values():
            handle.cache.max_age_seconds = self.settings.cache_ttl_seconds
        self.executor.settings = self.settings.execution_settings()

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
            self.catalog = self._build_catalog(self.chain_store.enabled())
            self.executor.update_catalog(self.catalog)

    def _build_catalog(self, chains: Iterable[SavedChainManifest]) -> ToolCatalog:
        return ToolCatalog.from_server_tools(
            {
                name: [
                    tool
                    for tool in handle.tools or []
                    if self.settings.tool_enabled(name, tool.name)
                ]
                for name, handle in self.handles.items()
            },
            self.handles.keys(),
            chains,
        )

    def _chain_dependencies(
        self,
        code: str,
        catalog: ToolCatalog,
    ) -> list[ChainDependency]:
        dependencies: dict[str, ChainDependency] = {}
        for public_name in self._referenced_callables(code, catalog):
            spec = catalog.tools[public_name]
            dependencies[public_name] = ChainDependency(
                kind=spec.kind,
                name=spec.name,
                call=spec.call,
                server=spec.server,
                schema_fingerprint=spec.schema_fingerprint,
            )
        return [dependencies[name] for name in sorted(dependencies)]

    def _chain_views(self) -> list[ChainStatusView]:
        called_by = self._called_by()
        views: list[ChainStatusView] = []
        for item in self.chain_store.load_all():
            chain = item.chain
            stale = [
                dependency.call
                for dependency in chain.dependencies
                if self._dependency_is_stale(dependency)
            ]
            status: Literal["ready", "disabled", "stale", "shadowed"]
            if self.chain_store.is_shadowed(item):
                status = "shadowed"
            elif not chain.enabled:
                status = "disabled"
            elif stale:
                status = "stale"
            else:
                status = "ready"
            views.append(
                ChainStatusView(
                    chain=chain,
                    scope=item.scope,
                    status=status,
                    stale_dependencies=stale,
                    called_by=called_by.get(chain.name, []) if status != "shadowed" else [],
                )
            )
        return views

    def _dependency_is_stale(self, dependency: ChainDependency) -> bool:
        spec = self.catalog.tools.get(dependency.name)
        if spec is not None:
            return spec.schema_fingerprint != dependency.schema_fingerprint
        if dependency.kind == "mcp_tool":
            handle = self.handles.get(dependency.server)
            if handle is not None and handle.tools is None:
                return False
        return True

    def _chain_view(self, name: str, scope: ChainScope) -> ChainStatusView:
        view = next(
            (
                view
                for view in self._chain_views()
                if view.chain.name == name and view.scope == scope
            ),
            None,
        )
        if view is None:
            raise ValueError(f"Unknown {scope} saved chain: {name}")
        return view

    def _called_by(self) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for item in self.chain_store.effective():
            chain = item.chain
            for dependency in chain.dependencies:
                if dependency.kind != "saved_chain":
                    continue
                target = dependency.name.removeprefix("chain_")
                if target == chain.name:
                    continue
                result.setdefault(target, []).append(chain.name)
        return {name: sorted(set(callers)) for name, callers in result.items()}

    def _required_servers_for_code(self, code: str) -> set[str]:
        required = self._referenced_servers(code)
        for public_name in self._referenced_callables(code, self.catalog):
            spec = self.catalog.tools[public_name]
            if spec.kind == "saved_chain":
                required.update(
                    self._required_servers_for_chain(self.chain_store.get(spec.backend_name).chain)
                )
        return required

    def _required_servers_for_chain(
        self,
        chain: SavedChainManifest,
        visited: set[str] | None = None,
    ) -> set[str]:
        seen = set() if visited is None else visited
        if chain.name in seen:
            return set()
        seen.add(chain.name)
        required: set[str] = set()
        manifests = {item.chain.name: item.chain for item in self.chain_store.effective()}
        for dependency in chain.dependencies:
            if dependency.kind == "mcp_tool":
                required.add(dependency.server)
                continue
            target = manifests.get(dependency.name.removeprefix("chain_"))
            if target is not None and target.enabled:
                required.update(self._required_servers_for_chain(target, seen))
        return required

    @staticmethod
    def _referenced_callables(code: str, catalog: ToolCatalog) -> set[str]:
        tree = _parse_code(code)
        if tree is None:
            return set()
        referenced: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            owner = node.func.value
            if not isinstance(owner, ast.Name):
                continue
            public_name = catalog.facade_calls.get((owner.id, node.func.attr))
            if public_name is not None:
                referenced.add(public_name)
        return referenced

    def _referenced_servers(self, code: str) -> set[str]:
        tree = _parse_code(code)
        if tree is None:
            return set()
        aliases = {alias: server for server, alias in self.catalog.server_aliases.items()}
        return {
            aliases[node.value.id]
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in aliases
        }


def _parse_code(code: str) -> ast.AST | None:
    normalized = textwrap.dedent(code).strip("\n")
    wrapped = f"async def __codemcp_main():\n{textwrap.indent(normalized, '    ')}\n"
    try:
        return ast.parse(wrapped, mode="exec")
    except SyntaxError:
        return None


def _compact_description(description: str | None, limit: int = 160) -> str | None:
    if description is None:
        return None
    compact = " ".join(description.split("\n\n", 1)[0].split())
    return compact if len(compact) <= limit else f"{compact[: limit - 1].rstrip()}…"


class RuntimeState:
    def __init__(self) -> None:
        self.runtime: GatewayRuntime | None = None
        self.paths: RuntimePaths | None = None


_runtime_state = RuntimeState()


def _require_runtime() -> GatewayRuntime:
    if _runtime_state.runtime is None:
        raise RuntimeError("Code Mode sidecar is not initialized")
    return _runtime_state.runtime


def configure_runtime_paths(paths: RuntimePaths | None) -> None:
    _runtime_state.paths = paths


def _runtime_paths() -> tuple[Path, Path, Path, Path, Path, Path | None]:
    return (_runtime_state.paths or resolve_runtime_paths()).as_tuple()


@asynccontextmanager
async def lifespan(_: FastMCP[None]) -> AsyncIterator[None]:
    (
        config_path,
        oauth_dir,
        catalog_dir,
        settings_path,
        global_chains_dir,
        project_chains_dir,
    ) = _runtime_paths()
    _runtime_state.runtime = GatewayRuntime.create(
        config_path,
        oauth_dir,
        catalog_dir,
        settings_path,
        global_chain_dir=global_chains_dir,
        project_chain_dir=project_chains_dir,
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
        "Search MCP tools and saved chains, then execute a typed sandboxed Python call graph."
    ),
    lifespan=lifespan,
)


@mcp.tool
async def search(
    query: str,
    limit: int = 5,
    server: str | None = None,
) -> SearchResponse:
    """Search configured upstream MCP tools and saved chains by capability."""
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
async def apply_manager_changes(
    changes: list[ChainEnabledChange],
) -> ManagerApplyResponse:
    """Apply staged settings and saved-chain enable changes with one catalog rebuild."""
    return await _require_runtime().apply_manager_changes(changes)


@mcp.tool
async def execute(code: str) -> ExecutionResponse:
    """Type-check and run one sandboxed Python MCP SDK chain."""
    return await _require_runtime().execute(code)


@mcp.tool
async def save_chain(
    name: str,
    description: str,
    code: str,
    input_schema: JsonObject,
    output_schema: JsonObject,
    scope: ChainScope = "project",
) -> SaveChainResponse:
    """Validate and persist one reusable typed MCP chain."""
    return await _require_runtime().chains.save(
        scope=scope,
        name=name,
        description=description,
        code=code,
        input_schema=json_types.JSON_OBJECT_ADAPTER.validate_python(input_schema),
        output_schema=json_types.JSON_OBJECT_ADAPTER.validate_python(output_schema),
    )


@mcp.tool
def list_chains() -> ChainListResponse:
    """List saved chains and their dependency state."""
    return _require_runtime().chains.list()


@mcp.tool
async def execute_chain(name: str, arguments: JsonObject) -> ExecutionResponse:
    """Execute one saved chain through its typed input contract."""
    validated_arguments = json_types.JSON_OBJECT_ADAPTER.validate_python(arguments)
    return await _require_runtime().chains.execute(name, validated_arguments)


@mcp.tool
async def revalidate_chain(name: str, scope: ChainScope) -> ChainStatusView:
    """Revalidate one scoped saved chain against the current callable catalog."""
    return await _require_runtime().chains.revalidate(name, scope)


@mcp.tool
async def delete_chain(name: str, scope: ChainScope) -> ChainListResponse:
    """Delete an unused saved chain from its storage scope."""
    return await _require_runtime().chains.delete(name, scope)


@mcp.tool
def status() -> StatusResponse:
    """Report cached catalog and upstream connection state without connecting upstreams."""
    return _require_runtime().status()


def main() -> None:
    mcp.run(transport="stdio", show_banner=False, log_level="ERROR")


if __name__ == "__main__":
    main()
