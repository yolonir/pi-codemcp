from __future__ import annotations

import ast
import asyncio
import textwrap
import time
from contextlib import AsyncExitStack, asynccontextmanager
from typing import TYPE_CHECKING, Literal, NamedTuple, Protocol, cast

import pydantic_monty
from fastmcp import Client, FastMCP
from fastmcp.mcp_config import RemoteMCPServer, StdioMCPServer
from pydantic import BaseModel, ConfigDict
from pydantic_core import to_json
from rapidfuzz import fuzz, process

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
    ExecutionLimitsView,
    InspectResponse,
    NormalizedServerInfo,
    SearchDetail,
    SearchMode,
    SearchResponse,
    ServerToolSummary,
    StatusResponse,
    UpstreamStatus,
    UpstreamToolStatus,
)
from .runtime_paths import resolve_runtime_paths
from .settings import CodeMcpSettings, load_settings
from .stats import StatsStore
from .tool_catalog import ToolCatalog, schema_path_summary

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
    from pathlib import Path

    from fastmcp.client.transports import ClientTransport
    from mcp import types as mcp_types

    from .runtime_paths import RuntimePaths

MAX_INSPECT_CALLS = 20
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
            client = cast(
                "Client[ClientTransport]",
                await exit_stack.enter_async_context(
                    Client(
                        self.server_config.to_transport(),
                        name=f"pi-codemcp-{self.info.name}",
                    )
                ),
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
        self.stats_store = StatsStore(settings_path.parent / "stats.json")
        for handle in handles.values():
            self.stats_store.record_cache(hit=handle.tools is not None)
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
        await self.stats_store.close()

    async def search(
        self,
        query: str | None = None,
        limit: int = 5,
        server: str | None = None,
        detail: SearchDetail = "signatures",
        mode: SearchMode = "search",
        cursor: int = 0,
    ) -> SearchResponse:
        started = time.perf_counter()
        input_bytes = len(
            to_json({
                "query": query,
                "limit": limit,
                "server": server,
                "detail": detail,
                "mode": mode,
                "cursor": cursor,
            })
        )
        try:
            response = await self._search_impl(query, limit, server, detail, mode, cursor)
        except BaseException:
            self.stats_store.record_operation(
                "search",
                duration_ms=_elapsed_ms(started),
                success=False,
                failure_stage="error",
                input_bytes=input_bytes,
            )
            raise
        self.stats_store.record_operation(
            "search",
            duration_ms=_elapsed_ms(started),
            success=True,
            input_bytes=input_bytes,
            output_bytes=len(to_json(response.model_dump(mode="json"))),
        )
        return response

    async def _search_impl(
        self,
        query: str | None,
        limit: int,
        server: str | None,
        detail: SearchDetail,
        mode: SearchMode,
        cursor: int,
    ) -> SearchResponse:
        self._validate_search_server(server)
        discovery_started = time.perf_counter()
        discovery_servers: Iterable[str]
        if server is None:
            discovery_servers = self.handles.keys()
        elif server == "chains":
            discovery_servers = ()
        else:
            discovery_servers = (server,)
        await self._ensure_servers_discovered(discovery_servers)
        self.stats_store.record_phase("discovery", _elapsed_ms(discovery_started))
        bounded_limit = min(max(limit, 1), 20)
        bounded_cursor = max(cursor, 0)
        counts = self.catalog.counts_by_server()
        servers = [
            ServerToolSummary(name=server_info.name, tool_count=counts[server_info.name])
            for server_info in self.normalized.servers
            if counts.get(server_info.name, 0) > 0
        ]
        if counts.get("chains", 0) > 0:
            servers.append(ServerToolSummary(name="chains", tool_count=counts["chains"]))
        filtered_count = counts.get(server, 0) if server is not None else len(self.catalog.tools)
        if mode == "inventory":
            results = self.catalog.inventory(
                server=server,
                detail=detail,
                offset=bounded_cursor,
                limit=bounded_limit,
            )
            total_matches = filtered_count
        else:
            clean_query = (query or "").strip()
            if not clean_query:
                raise ValueError("query is required in search mode")
            all_matches = self.catalog.search(
                clean_query,
                filtered_count,
                server=server,
                detail=detail,
            )
            total_matches = len(all_matches)
            results = all_matches[bounded_cursor : bounded_cursor + bounded_limit]
        next_cursor = (
            bounded_cursor + len(results) if bounded_cursor + len(results) < total_matches else None
        )
        include_prelude = detail == "full"
        if mode == "search" and detail == "signatures" and results:
            inspected = {
                item.call: item.stub
                for item in self.catalog.inspect([
                    result.call for result in results[: min(3, len(results))]
                ])
            }
            results = [
                result.model_copy(update={"stub": inspected[result.call]})
                if result.call in inspected
                else result
                for result in results
            ]
            include_prelude = True
        return SearchResponse(
            mode=mode,
            detail=detail,
            total_tool_count=len(self.catalog.tools),
            filtered_tool_count=filtered_count,
            servers=servers,
            cursor=bounded_cursor,
            next_cursor=next_cursor,
            has_more=next_cursor is not None,
            project_scope_available=self.chain_store.project_store is not None,
            execution_limits=self._execution_limits_view(),
            prelude=self.catalog.stub_prelude if include_prelude else None,
            results=results,
        )

    async def inspect(self, calls: list[str]) -> InspectResponse:
        started = time.perf_counter()
        try:
            response = await self._inspect_impl(calls)
        except BaseException:
            self.stats_store.record_operation(
                "inspect",
                duration_ms=_elapsed_ms(started),
                success=False,
                failure_stage="error",
                input_bytes=len(to_json(calls)),
            )
            raise
        self.stats_store.record_operation(
            "inspect",
            duration_ms=_elapsed_ms(started),
            success=True,
            input_bytes=len(to_json(calls)),
            output_bytes=len(to_json(response.model_dump(mode="json"))),
        )
        return response

    async def _inspect_impl(self, calls: list[str]) -> InspectResponse:
        if not calls:
            raise ValueError("calls must contain at least one MCP call")
        if len(calls) > MAX_INSPECT_CALLS:
            raise ValueError(f"calls must contain at most {MAX_INSPECT_CALLS} MCP calls")
        discovery_started = time.perf_counter()
        requested_namespaces = {call.partition(".")[0] for call in calls}
        discovery_servers = [
            server
            for server, namespace in self.catalog.server_aliases.items()
            if namespace in requested_namespaces
        ]
        await self._ensure_servers_discovered(discovery_servers)
        self.stats_store.record_phase("discovery", _elapsed_ms(discovery_started))
        return InspectResponse(
            prelude=self.catalog.stub_prelude,
            project_scope_available=self.chain_store.project_store is not None,
            execution_limits=self._execution_limits_view(),
            results=self.catalog.inspect(calls),
        )

    def _validate_search_server(self, server: str | None) -> None:
        if server is None:
            return
        valid = set(self.catalog.servers)
        if server in valid:
            return
        suggestions = [
            name
            for name, _score, _index in process.extract(
                server,
                sorted(valid),
                scorer=fuzz.ratio,
                limit=3,
            )
        ]
        raise ValueError(
            f"Unknown MCP server {server!r}; available: {sorted(valid)}; suggestions: {suggestions}"
        )

    def _execution_limits_view(self) -> ExecutionLimitsView:
        settings = self.executor.settings
        return ExecutionLimitsView(
            timeout_seconds=settings.timeout_seconds,
            tool_timeout_seconds=settings.tool_timeout_seconds,
            max_calls=settings.max_calls,
            result_limit_bytes=settings.result_byte_limit,
        )

    async def execute(self, code: str) -> ExecutionResponse:
        started = time.perf_counter()
        input_bytes = len(code.encode())
        discovery_started = time.perf_counter()
        try:
            await self._ensure_servers_discovered(self._required_servers_for_code(code))
        except BaseException:
            self.stats_store.record_phase("discovery", _elapsed_ms(discovery_started))
            self._record_operation_exception(
                "execute",
                started,
                input_bytes,
                failure_stage="discovery",
            )
            raise
        self.stats_store.record_phase("discovery", _elapsed_ms(discovery_started))
        self.executor.update_catalog(self.catalog)
        try:
            response = await self.executor.execute_graph(code, self._dispatch)
        except BaseException as error:
            self._record_operation_exception(
                "execute",
                started,
                input_bytes,
                failure_stage=(
                    "cancelled" if isinstance(error, asyncio.CancelledError) else "error"
                ),
            )
            raise
        self._record_execution("execute", started, input_bytes, response)
        return response

    async def _execute_chain(self, name: str, arguments: JsonObject) -> ExecutionResponse:
        started = time.perf_counter()
        input_bytes = len(to_json(arguments))
        try:
            chain = self.chain_store.get(name).chain
        except BaseException:
            self._record_operation_exception(
                "execute_chain",
                started,
                input_bytes,
                failure_stage="preflight",
            )
            raise
        if not chain.enabled:
            response = ExecutionResponse(
                ok=False,
                failure_stage="preflight",
                error=f"Saved chain is disabled: {name}",
            )
            self._record_execution(
                "execute_chain",
                started,
                input_bytes,
                response,
            )
            return response
        discovery_started = time.perf_counter()
        try:
            await self._ensure_servers_discovered(self._required_servers_for_chain(chain))
        except BaseException:
            self.stats_store.record_phase("discovery", _elapsed_ms(discovery_started))
            self._record_operation_exception(
                "execute_chain",
                started,
                input_bytes,
                failure_stage="discovery",
            )
            raise
        self.stats_store.record_phase("discovery", _elapsed_ms(discovery_started))
        self.executor.update_catalog(self.catalog)
        try:
            response = await self.executor.execute_saved_chain(chain, arguments, self._dispatch)
        except BaseException as error:
            self._record_operation_exception(
                "execute_chain",
                started,
                input_bytes,
                failure_stage=(
                    "cancelled" if isinstance(error, asyncio.CancelledError) else "error"
                ),
            )
            raise
        self._record_execution(
            "execute_chain",
            started,
            input_bytes,
            response,
        )
        return response

    def _record_operation_exception(
        self,
        operation: str,
        started: float,
        input_bytes: int,
        *,
        failure_stage: str,
    ) -> None:
        self.stats_store.record_operation(
            operation,
            duration_ms=_elapsed_ms(started),
            success=False,
            failure_stage=failure_stage,
            input_bytes=input_bytes,
        )

    def _record_execution(
        self,
        operation: str,
        started: float,
        input_bytes: int,
        response: ExecutionResponse,
    ) -> None:
        self.stats_store.record_operation(
            operation,
            duration_ms=_elapsed_ms(started),
            success=response.ok,
            failure_stage=response.failure_stage,
            input_bytes=input_bytes,
            output_bytes=response.metrics.result_bytes,
            calls=response.calls_made,
            chain_calls=response.chain_calls,
        )
        metrics = response.metrics
        for phase, duration in (
            ("typecheck", metrics.typecheck_ms),
            ("execution", metrics.runtime_ms),
            ("serialization", metrics.serialization_ms),
        ):
            if duration > 0:
                self.stats_store.record_phase(phase, duration)

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
        started = time.perf_counter()
        input_bytes = len(
            to_json({
                "scope": scope,
                "name": name,
                "description": description,
                "code": code,
                "input_schema": input_schema,
                "output_schema": output_schema,
            })
        )
        try:
            response = await self._save_chain_impl(
                scope=scope,
                name=name,
                description=description,
                code=code,
                input_schema=input_schema,
                output_schema=output_schema,
            )
        except BaseException:
            self.stats_store.record_operation(
                "save_chain",
                duration_ms=_elapsed_ms(started),
                success=False,
                failure_stage="validation",
                input_bytes=input_bytes,
            )
            raise
        self.stats_store.record_operation(
            "save_chain",
            duration_ms=_elapsed_ms(started),
            success=True,
            input_bytes=input_bytes,
            output_bytes=len(to_json(response.model_dump(mode="json"))),
        )
        return response

    async def _save_chain_impl(
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
        except (
            pydantic_monty.MontyTypingError,
            pydantic_monty.MontySyntaxError,
            pydantic_monty.MontyRuntimeError,
            NotImplementedError,
            RuntimeError,
        ) as error:
            if isinstance(error, pydantic_monty.MontyTypingError):
                message = error.display("concise", color=False).strip()
            elif isinstance(
                error,
                (pydantic_monty.MontySyntaxError, pydantic_monty.MontyRuntimeError),
            ):
                message = error.display("type-msg").strip()
            else:
                message = f"Sandbox compilation failed: {error}"
            raise ValueError(_saved_chain_preflight_error(message, output_schema)) from error
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
        started = time.perf_counter()
        response = ChainListResponse(chains=self._chain_views())
        self.stats_store.record_operation(
            "list_chains",
            duration_ms=_elapsed_ms(started),
            success=True,
            output_bytes=len(to_json(response.model_dump(mode="json"))),
        )
        return response

    async def _revalidate_chain(self, name: str, scope: ChainScope) -> ChainStatusView:
        started = time.perf_counter()
        try:
            response = await self._revalidate_chain_impl(name, scope)
        except BaseException:
            self.stats_store.record_operation(
                "revalidate_chain",
                duration_ms=_elapsed_ms(started),
                success=False,
                failure_stage="validation",
            )
            raise
        self.stats_store.record_operation(
            "revalidate_chain",
            duration_ms=_elapsed_ms(started),
            success=True,
            output_bytes=len(to_json(response.model_dump(mode="json"))),
        )
        return response

    async def _revalidate_chain_impl(self, name: str, scope: ChainScope) -> ChainStatusView:
        current = self.chain_store.get(name, scope).chain
        await self._ensure_servers_discovered(self._referenced_servers(current.code))
        chains = [chain for chain in self.chain_store.enabled() if chain.name != name]
        chains.append(current)
        candidate_catalog = self._build_catalog(chains)
        spec = candidate_catalog.tools[current.public_name]
        try:
            await self.executor.validate_saved_chain(current.code, candidate_catalog, spec)
        except (
            pydantic_monty.MontyTypingError,
            pydantic_monty.MontySyntaxError,
            pydantic_monty.MontyRuntimeError,
            NotImplementedError,
            RuntimeError,
        ) as error:
            if isinstance(error, pydantic_monty.MontyTypingError):
                message = error.display("concise", color=False).strip()
            elif isinstance(
                error,
                (pydantic_monty.MontySyntaxError, pydantic_monty.MontyRuntimeError),
            ):
                message = error.display("type-msg").strip()
            else:
                message = f"Sandbox compilation failed: {error}"
            raise ValueError(
                _saved_chain_preflight_error(message, current.output_schema)
            ) from error
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
        started = time.perf_counter()
        try:
            response = await self._delete_chain_impl(name, scope)
        except BaseException:
            self.stats_store.record_operation(
                "delete_chain",
                duration_ms=_elapsed_ms(started),
                success=False,
                failure_stage="error",
            )
            raise
        self.stats_store.record_operation(
            "delete_chain",
            duration_ms=_elapsed_ms(started),
            success=True,
            output_bytes=len(to_json(response.model_dump(mode="json"))),
        )
        return response

    async def _delete_chain_impl(self, name: str, scope: ChainScope) -> ChainListResponse:
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
        started = time.perf_counter()
        input_bytes = len(to_json(arguments))
        try:
            result = await handle.call_tool(
                spec.backend_name,
                arguments,
                timeout_seconds=min(
                    self.executor.settings.tool_timeout_seconds,
                    max(0.001, context.remaining_seconds()),
                ),
            )
            normalized = context.catalog.normalize_result(public_name, result)
        except BaseException:
            self.stats_store.record_upstream(
                spec.server,
                spec.backend_name,
                duration_ms=_elapsed_ms(started),
                success=False,
                input_bytes=input_bytes,
                output_bytes=0,
            )
            raise
        self.stats_store.record_upstream(
            spec.server,
            spec.backend_name,
            duration_ms=_elapsed_ms(started),
            success=True,
            input_bytes=input_bytes,
            output_bytes=len(to_json(normalized)),
        )
        return normalized

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


def _saved_chain_preflight_error(message: str, output_schema: JsonObject) -> str:
    expected = "\n".join(f"  - {path}" for path in schema_path_summary(output_schema))
    return (
        "Saved chain failed preflight against outputSchema.\n"
        f"Expected output paths:\n{expected}\n"
        f"Actual type-check result:\n{message}"
    )


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1_000


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
    runtime = GatewayRuntime.create(
        config_path,
        oauth_dir,
        catalog_dir,
        settings_path,
        global_chain_dir=global_chains_dir,
        project_chain_dir=project_chains_dir,
    )
    _runtime_state.runtime = runtime
    try:
        yield
    finally:
        _runtime_state.runtime = None
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
    query: str | None = None,
    limit: int = 5,
    server: str | None = None,
    detail: SearchDetail = "signatures",
    mode: SearchMode = "search",
    cursor: int = 0,
) -> SearchResponse:
    """Search or page through configured upstream MCP tools and saved chains."""
    return await _require_runtime().search(query, limit, server, detail, mode, cursor)


@mcp.tool
async def inspect(calls: list[str]) -> InspectResponse:
    """Return exact typed SDK stubs for selected call identifiers."""
    return await _require_runtime().inspect(calls)


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
def stats() -> JsonObject:
    """Return bounded local CodeMCP telemetry rollups."""
    return _require_runtime().stats_store.snapshot()


@mcp.tool
def status() -> StatusResponse:
    """Report cached catalog and upstream connection state without connecting upstreams."""
    return _require_runtime().status()


def main() -> None:
    mcp.run(transport="stdio", show_banner=False, log_level="ERROR")


if __name__ == "__main__":
    main()
