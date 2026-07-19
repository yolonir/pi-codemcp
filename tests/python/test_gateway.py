from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from sidecar import gateway
from sidecar.chains import ChainEnabledChange
from sidecar.json_types import JSON_OBJECT_ADAPTER, JsonObject, JsonValue


def structured_data(structured: dict[str, object] | None) -> JsonObject:
    assert structured is not None
    return JSON_OBJECT_ADAPTER.validate_python(structured)


def json_object_list(value: JsonValue) -> list[JsonObject]:
    assert isinstance(value, list)
    objects: list[JsonObject] = []
    for item in value:
        assert isinstance(item, dict)
        objects.append(item)
    return objects


def objects_by_name(value: JsonValue) -> dict[str, JsonObject]:
    objects: dict[str, JsonObject] = {}
    for item in json_object_list(value):
        name = item.get("name")
        assert isinstance(name, str)
        objects[name] = item
    return objects


def required_string(record: JsonObject, key: str) -> str:
    value = record[key]
    assert isinstance(value, str)
    return value


def required_object(record: JsonObject, key: str) -> JsonObject:
    value = record[key]
    assert isinstance(value, dict)
    return value


async def wait_for_process_exit(pid: int) -> None:
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"upstream child process {pid} was not terminated")


def write_config(
    path: Path,
    fixture: Path,
    alpha_pid: Path,
    beta_pid: Path,
    *,
    alpha_cache_buster: str | None = None,
) -> None:
    alpha_env = {"TEST_PID_FILE": str(alpha_pid)}
    if alpha_cache_buster is not None:
        alpha_env["CACHE_BUSTER"] = alpha_cache_buster
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "alpha": {
                        "command": sys.executable,
                        "args": [str(fixture), "alpha"],
                        "env": alpha_env,
                    },
                    "beta": {
                        "command": sys.executable,
                        "args": [str(fixture), "beta"],
                        "env": {"TEST_PID_FILE": str(beta_pid)},
                    },
                    "ignored": {"command": "never", "disabled": True},
                }
            }
        ),
        encoding="utf-8",
    )


def test_runtime_paths_honor_pi_agent_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_chains_path = tmp_path / "workspace" / ".pi" / "pi-codemcp" / "chains"
    monkeypatch.delenv("PI_CODEMCP_AGENT_DIR", raising=False)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path))
    monkeypatch.setenv("PI_CODEMCP_PROJECT_CHAINS_DIR", str(project_chains_path))

    config, oauth, catalog, settings, global_chains, project_chains = (
        gateway._runtime_paths()
    )

    assert config == tmp_path / "mcp.json"
    assert oauth == tmp_path / "pi-codemcp" / "oauth"
    assert catalog == tmp_path / "pi-codemcp" / "catalog"
    assert settings == tmp_path / "pi-codemcp" / "settings.json"
    assert global_chains == tmp_path / "pi-codemcp" / "chains"
    assert project_chains == project_chains_path


def configure_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    config_path: Path,
) -> None:
    assert config_path == tmp_path / "mcp.json"
    monkeypatch.setenv("PI_CODEMCP_AGENT_DIR", str(tmp_path))


def test_gateway_reports_an_all_disabled_config(tmp_path: Path) -> None:
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        json.dumps({"mcpServers": {"only": {"command": "unused", "disabled": True}}})
    )

    runtime = gateway.GatewayRuntime.create(
        config_path,
        tmp_path / "oauth",
        tmp_path / "catalog",
    )

    assert runtime.status().model_dump()["upstreams"] == [
        {
            "name": "only",
            "transport": "stdio",
            "enabled": False,
            "connected": False,
            "discovered": False,
            "auth": None,
            "tool_count": 0,
            "total_tool_count": 0,
            "tools": [],
        }
    ]


@pytest.mark.asyncio
async def test_force_discover_refreshes_only_the_selected_server(
    tmp_path: Path,
) -> None:
    root = Path(__file__).parents[2]
    fixture = root / "tests" / "fixtures" / "upstream_server.py"
    alpha_pid = tmp_path / "alpha.pid"
    beta_pid = tmp_path / "beta.pid"
    config_path = tmp_path / "mcp.json"
    write_config(config_path, fixture, alpha_pid, beta_pid)
    runtime = gateway.GatewayRuntime.create(
        config_path,
        tmp_path / "oauth",
        tmp_path / "catalog",
    )

    try:
        first_status = await runtime.discover("beta")
        assert first_status.tool_count == 1
        assert (
            next(
                upstream
                for upstream in first_status.upstreams
                if upstream.name == "beta"
            ).discovered
            is True
        )
        assert not alpha_pid.exists()
        first_pid = int(beta_pid.read_text())
        await wait_for_process_exit(first_pid)
        beta_pid.unlink()

        runtime.settings_path.write_text(
            json.dumps({"disabledTools": {"beta": ["save_number"]}})
        )
        policy_status = await runtime.reload_settings()
        beta_status = next(
            upstream for upstream in policy_status.upstreams if upstream.name == "beta"
        )
        assert beta_status.tool_count == 0
        assert beta_status.total_tool_count == 1
        assert beta_status.tools[0].enabled is False
        assert runtime.catalog.search("save number") == []
        blocked = await runtime.execute('return await beta.save_number({"value": 1})')
        assert blocked.ok is False
        assert blocked.failure_stage == "preflight"

        second_status = await runtime.discover("beta")
        assert second_status.tool_count == 0
        assert beta_pid.exists()
        second_pid = int(beta_pid.read_text())
        await wait_for_process_exit(second_pid)
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_scoped_search_validates_before_discovery_and_connects_only_target(
    tmp_path: Path,
) -> None:
    root = Path(__file__).parents[2]
    fixture = root / "tests" / "fixtures" / "upstream_server.py"
    alpha_pid = tmp_path / "alpha.pid"
    beta_pid = tmp_path / "beta.pid"
    config_path = tmp_path / "mcp.json"
    write_config(config_path, fixture, alpha_pid, beta_pid)
    runtime = gateway.GatewayRuntime.create(
        config_path,
        tmp_path / "oauth",
        tmp_path / "catalog",
    )

    try:
        with pytest.raises(ValueError, match="suggestions"):
            await runtime.search("number", server="bet")
        assert not alpha_pid.exists()
        assert not beta_pid.exists()

        response = await runtime.search("number", server="beta")
        assert [item.call for item in response.results] == ["beta.save_number"]
        assert not alpha_pid.exists()
        assert beta_pid.exists()
        await wait_for_process_exit(int(beta_pid.read_text()))
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_gateway_lazy_connections_cache_facade_and_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).parents[2]
    fixture = root / "tests" / "fixtures" / "upstream_server.py"
    alpha_pid = tmp_path / "alpha.pid"
    beta_pid = tmp_path / "beta.pid"
    config_path = tmp_path / "mcp.json"
    write_config(config_path, fixture, alpha_pid, beta_pid)
    configure_environment(monkeypatch, tmp_path, config_path)

    client = Client(gateway.mcp)
    async with client:
        exposed = {tool.name for tool in await client.list_tools()}
        assert exposed == {
            "search",
            "inspect",
            "discover",
            "reload_settings",
            "apply_manager_changes",
            "execute",
            "save_chain",
            "list_chains",
            "execute_chain",
            "revalidate_chain",
            "delete_chain",
            "stats",
            "status",
        }

        initial_status = await client.call_tool("status", {})
        initial_data = structured_data(initial_status.structured_content)
        assert initial_data["tool_count"] == 0
        upstreams = objects_by_name(initial_data["upstreams"])
        assert upstreams["alpha"]["enabled"] is True
        assert upstreams["alpha"]["connected"] is False
        assert upstreams["alpha"]["discovered"] is False
        assert upstreams["ignored"] == {
            "name": "ignored",
            "transport": "stdio",
            "enabled": False,
            "connected": False,
            "discovered": False,
            "auth": None,
            "tool_count": 0,
            "total_tool_count": 0,
            "tools": [],
        }
        assert not alpha_pid.exists()
        assert not beta_pid.exists()

        search = await client.call_tool("search", {"query": "save number"})
        search_data = structured_data(search.structured_content)
        assert search_data["total_tool_count"] == 3
        assert search_data["servers"] == [
            {"name": "alpha", "tool_count": 2},
            {"name": "beta", "tool_count": 1},
        ]
        search_results = json_object_list(search_data["results"])
        assert search_results[0]["name"] == "beta_save_number"
        assert search_results[0]["call"] == "beta.save_number"
        assert "call_tool" not in required_string(search_results[0], "signature")
        beta_search = await client.call_tool(
            "search", {"query": "number", "server": "beta"}
        )
        beta_search_data = structured_data(beta_search.structured_content)
        beta_search_results = json_object_list(beta_search_data["results"])
        assert [item["name"] for item in beta_search_results] == ["beta_save_number"]
        assert beta_search_data["detail"] == "signatures"
        assert beta_search_data["project_scope_available"] is False
        execution_limits = required_object(beta_search_data, "execution_limits")
        assert execution_limits["max_calls"] == 50

        inventory = await client.call_tool(
            "search",
            {"mode": "inventory", "detail": "names", "limit": 1, "cursor": 0},
        )
        inventory_data = structured_data(inventory.structured_content)
        assert inventory_data["has_more"] is True
        assert inventory_data["next_cursor"] == 1
        inventory_results = json_object_list(inventory_data["results"])
        assert "signature" not in inventory_results[0]

        with pytest.raises(ToolError, match="suggestions"):
            await client.call_tool("search", {"query": "number", "server": "bet"})

        discovery_pids = [int(alpha_pid.read_text()), int(beta_pid.read_text())]
        for pid in discovery_pids:
            await wait_for_process_exit(pid)
        alpha_pid.unlink()
        beta_pid.unlink()

        assert "JsonValue: TypeAlias" in required_string(search_data, "prelude")
        assert "BetaSaveNumberArgs" in required_string(search_results[0], "stub")
        assert all("stub" in item for item in search_results[:3])
        assert all("stub" not in item for item in search_results[3:])
        inspected = await client.call_tool("inspect", {"calls": ["alpha.get_number"]})
        inspected_data = structured_data(inspected.structured_content)
        inspected_results = json_object_list(inspected_data["results"])
        assert "JsonValue: TypeAlias" in required_string(inspected_data, "prelude")
        assert "AlphaGetNumberArgs" in required_string(inspected_results[0], "stub")
        assert "input_schema" not in search_results[0]
        assert "output_schema" not in search_results[0]
        assert not alpha_pid.exists()
        assert not beta_pid.exists()

        alpha_only = await client.call_tool(
            "execute",
            {"code": 'number = await alpha.get_number({"seed": 4})\nreturn number'},
        )
        alpha_data = structured_data(alpha_only.structured_content)
        assert alpha_data["ok"] is True
        assert alpha_data["result"] == {"value": 5}
        assert alpha_pid.exists()
        assert not beta_pid.exists()

        executed = await client.call_tool(
            "execute",
            {
                "code": """
                number = await alpha.get_number({"seed": 4})
                saved = await beta.save_number({"value": number["value"]})
                return {"identifier": saved["identifier"], "saved": saved["saved"]}
                """
            },
        )
        execution_data = structured_data(executed.structured_content)
        assert execution_data["ok"] is True
        assert execution_data["result"] == {"identifier": "N-5", "saved": True}
        assert execution_data["calls_made"] == 2
        timings = required_object(execution_data, "timings")
        assert set(timings) == {
            "typecheck_ms",
            "execution_ms",
            "serialization_ms",
        }
        assert all(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and value >= 0
            for value in timings.values()
        )
        assert beta_pid.exists()
        connected_pids = [int(alpha_pid.read_text()), int(beta_pid.read_text())]

    assert gateway._runtime_state.runtime is None
    for pid in connected_pids:
        await wait_for_process_exit(pid)

    alpha_pid.unlink()
    beta_pid.unlink()
    cached_client = Client(gateway.mcp)
    async with cached_client:
        cached_status = await cached_client.call_tool("status", {})
        cached_data = structured_data(cached_status.structured_content)
        assert cached_data["tool_count"] == 3
        await cached_client.call_tool("search", {"query": "number"})
        assert not alpha_pid.exists()
        assert not beta_pid.exists()


@pytest.mark.asyncio
async def test_execute_telemetry_records_discovery_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "mcp.json"
    config_path.write_text(json.dumps({"mcpServers": {}}))
    runtime = gateway.GatewayRuntime.create(
        config_path,
        tmp_path / "oauth",
        tmp_path / "catalog",
    )

    async def fail_discovery(_: object) -> None:
        raise RuntimeError("discovery failed")

    monkeypatch.setattr(runtime, "_ensure_servers_discovered", fail_discovery)
    try:
        with pytest.raises(RuntimeError, match="discovery failed"):
            await runtime.execute("return 1")
        snapshot = runtime.stats_store.snapshot()
        operations = snapshot["operations"]
        failures = snapshot["failures"]
        assert isinstance(operations, dict)
        assert isinstance(failures, dict)
        execute = operations["execute"]
        assert isinstance(execute, dict)
        assert execute["count"] == 1
        assert execute["failure"] == 1
        assert failures["discovery"] == 1
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_first_execute_discovers_and_connects_only_referenced_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).parents[2]
    fixture = root / "tests" / "fixtures" / "upstream_server.py"
    alpha_pid = tmp_path / "alpha.pid"
    beta_pid = tmp_path / "beta.pid"
    config_path = tmp_path / "mcp.json"
    write_config(config_path, fixture, alpha_pid, beta_pid)
    configure_environment(monkeypatch, tmp_path, config_path)

    client = Client(gateway.mcp)
    async with client:
        executed = structured_data(
            (
                await client.call_tool(
                    "execute",
                    {
                        "code": 'value = await alpha.get_number({"seed": 2})\n'
                        'return value["value"]'
                    },
                )
            ).structured_content
        )
        assert executed["ok"] is True
        assert executed["result"] == 3
        assert alpha_pid.exists()
        assert not beta_pid.exists()
        alpha_process = int(alpha_pid.read_text())
    await wait_for_process_exit(alpha_process)


@pytest.mark.asyncio
async def test_catalog_cache_invalidates_only_changed_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).parents[2]
    fixture = root / "tests" / "fixtures" / "upstream_server.py"
    alpha_pid = tmp_path / "alpha.pid"
    beta_pid = tmp_path / "beta.pid"
    config_path = tmp_path / "mcp.json"
    write_config(config_path, fixture, alpha_pid, beta_pid)
    configure_environment(monkeypatch, tmp_path, config_path)

    first = Client(gateway.mcp)
    async with first:
        await first.call_tool("search", {"query": "number"})
        initial_pids = [int(alpha_pid.read_text()), int(beta_pid.read_text())]
    for pid in initial_pids:
        await wait_for_process_exit(pid)
    alpha_pid.unlink()
    beta_pid.unlink()

    write_config(
        config_path,
        fixture,
        alpha_pid,
        beta_pid,
        alpha_cache_buster="changed",
    )
    second = Client(gateway.mcp)
    async with second:
        status = structured_data(
            (await second.call_tool("status", {})).structured_content
        )
        counts = {
            name: item["tool_count"]
            for name, item in objects_by_name(status["upstreams"]).items()
        }
        assert counts == {"alpha": 0, "beta": 1, "ignored": 0}
        await second.call_tool("search", {"query": "number"})
        assert alpha_pid.exists()
        assert not beta_pid.exists()
        changed_pid = int(alpha_pid.read_text())
    await wait_for_process_exit(changed_pid)


@pytest.mark.asyncio
async def test_saved_chains_are_typed_composable_and_recursion_is_bounded(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        json.dumps({"mcpServers": {"unused": {"command": "unused", "disabled": True}}})
    )
    runtime = gateway.GatewayRuntime.create(
        config_path,
        tmp_path / "oauth",
        tmp_path / "catalog",
    )
    integer_input: JsonObject = {
        "type": "object",
        "properties": {"count": {"type": "integer", "minimum": 0}},
        "required": ["count"],
        "additionalProperties": False,
    }
    integer_output: JsonObject = {
        "type": "object",
        "properties": {"value": {"type": "integer"}},
        "required": ["value"],
        "additionalProperties": False,
    }

    try:
        with pytest.raises(ValueError) as invalid_chain:
            await runtime.chains.save(
                scope="global",
                name="invalid_countdown",
                description="Return an invalid output shape.",
                code='return {"value": "wrong"}',
                input_schema=integer_input,
                output_schema=integer_output,
            )
        validation_message = str(invalid_chain.value)
        assert "failed preflight against outputSchema" in validation_message
        assert "$.value: integer (required)" in validation_message
        assert "Actual type-check result" in validation_message

        saved = await runtime.chains.save(
            scope="global",
            name="countdown",
            description="Recursively count down to zero and rebuild the total.",
            code="""
            if input["count"] == 0:
                return {"value": 0}
            previous = await chains.countdown({"count": input["count"] - 1})
            return {"value": previous["value"] + 1}
            """,
            input_schema=integer_input,
            output_schema=integer_output,
        )
        assert saved.created is True
        assert saved.chain.scope == "global"
        assert saved.chain.status == "ready"
        assert saved.chain.chain.native_tool == "mcp_chain_countdown"
        assert [dependency.call for dependency in saved.chain.chain.dependencies] == [
            "chains.countdown"
        ]

        native = await runtime.chains.execute("countdown", {"count": 3})
        assert native.ok is True
        assert native.result == {"value": 3}
        assert native.calls_made == 0
        assert native.chain_calls == 3

        composed = await runtime.execute(
            'result = await chains.countdown({"count": 2})\nreturn result["value"]'
        )
        assert composed.ok is True
        assert composed.result == 2
        assert composed.chain_calls == 3

        runtime.executor.settings.max_chain_depth = 4
        too_deep = await runtime.chains.execute("countdown", {"count": 5})
        assert too_deep.ok is False
        assert too_deep.failure_stage == "runtime"
        assert too_deep.error and "recursion depth exceeded 4" in too_deep.error
        runtime.executor.settings.max_chain_depth = 16

        await runtime.chains.save(
            scope="global",
            name="double_countdown",
            description="Compose the countdown chain twice.",
            code="""
            first = await chains.countdown({"count": input["count"]})
            second = await chains.countdown({"count": input["count"]})
            return {"value": first["value"] + second["value"]}
            """,
            input_schema=integer_input,
            output_schema=integer_output,
        )
        chained = await runtime.chains.execute("double_countdown", {"count": 2})
        assert chained.ok is True
        assert chained.result == {"value": 4}
        assert chained.chain_calls == 6

        matches = await runtime.search("countdown")
        assert {summary.name: summary.tool_count for summary in matches.servers} == {
            "chains": 2
        }
        assert {match.call for match in matches.results} == {
            "chains.countdown",
            "chains.double_countdown",
        }
        assert all(match.source == "saved_chain" for match in matches.results)

        with pytest.raises(ValueError, match="used by: double_countdown"):
            await runtime.chains.delete("countdown", "global")

        applied = await runtime.apply_manager_changes(
            [ChainEnabledChange(name="countdown", scope="global", enabled=False)]
        )
        disabled = next(
            view
            for view in applied.chains
            if view.chain.name == "countdown" and view.scope == "global"
        )
        assert disabled.status == "disabled"
        blocked = await runtime.chains.execute("countdown", {"count": 1})
        assert blocked.ok is False
        assert blocked.failure_stage == "preflight"
        dependent = next(
            view
            for view in runtime.chains.list().chains
            if view.chain.name == "double_countdown"
        )
        assert dependent.status == "stale"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_saved_chain_nested_generated_output_types_execute_without_name_errors(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "mcp.json"
    config_path.write_text(json.dumps({"mcpServers": {}}))
    runtime = gateway.GatewayRuntime.create(
        config_path,
        tmp_path / "oauth",
        tmp_path / "catalog",
    )
    nested_output: JsonObject = {
        "type": "object",
        "properties": {
            "positions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string"},
                        "legs": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "exchange": {"type": "string"},
                                    "size": {"type": "number"},
                                },
                                "required": ["exchange", "size"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["symbol", "legs"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["positions"],
        "additionalProperties": False,
    }
    try:
        await runtime.chains.save(
            scope="global",
            name="nested_positions",
            description="Return nested generated output types.",
            code='return {"positions": [{"symbol": "BTC", "legs": [{"exchange": "alpha", "size": 1.5}]}]}',
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            output_schema=nested_output,
        )
        native = await runtime.chains.execute("nested_positions", {})
        assert native.ok is True
        assert native.result == {
            "positions": [
                {"symbol": "BTC", "legs": [{"exchange": "alpha", "size": 1.5}]}
            ]
        }
        nested = await runtime.execute("return await chains.nested_positions({})")
        assert nested.ok is True
        assert nested.result == native.result
        assert not (nested.error and "NameError" in nested.error)
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_project_chains_shadow_global_chains_without_disabled_fallback(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "mcp.json"
    config_path.write_text(json.dumps({"mcpServers": {}}))
    runtime = gateway.GatewayRuntime.create(
        config_path,
        tmp_path / "oauth",
        tmp_path / "catalog",
        project_chain_dir=tmp_path / "project" / ".pi" / "pi-codemcp" / "chains",
    )
    input_schema: JsonObject = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    output_schema: JsonObject = {
        "type": "object",
        "properties": {"source": {"type": "string"}},
        "required": ["source"],
        "additionalProperties": False,
    }

    try:
        await runtime.chains.save(
            scope="global",
            name="source",
            description="Return the global source.",
            code='return {"source": "global"}',
            input_schema=input_schema,
            output_schema=output_schema,
        )
        await runtime.chains.save(
            scope="project",
            name="source",
            description="Return the project source.",
            code='return {"source": "project"}',
            input_schema=input_schema,
            output_schema=output_schema,
        )

        views = runtime.chains.list().chains
        assert [(view.scope, view.status) for view in views] == [
            ("project", "ready"),
            ("global", "shadowed"),
        ]
        project_result = await runtime.chains.execute("source", {})
        assert project_result.ok is True
        assert project_result.result == {"source": "project"}

        (tmp_path / "settings.json").write_text(json.dumps({"maxCalls": 17}))
        applied = await runtime.apply_manager_changes(
            [ChainEnabledChange(name="source", scope="project", enabled=False)]
        )
        assert runtime.settings.max_calls == 17
        assert applied.status.connected is True
        assert [(view.scope, view.status) for view in applied.chains] == [
            ("project", "disabled"),
            ("global", "shadowed"),
        ]
        assert [(view.scope, view.status) for view in runtime.chains.list().chains] == [
            ("project", "disabled"),
            ("global", "shadowed"),
        ]
        blocked = await runtime.chains.execute("source", {})
        assert blocked.ok is False
        assert blocked.error == "Saved chain is disabled: source"

        await runtime.chains.delete("source", "project")
        global_result = await runtime.chains.execute("source", {})
        assert global_result.ok is True
        assert global_result.result == {"source": "global"}
        assert [(view.scope, view.status) for view in runtime.chains.list().chains] == [
            ("global", "ready")
        ]
    finally:
        await runtime.close()
