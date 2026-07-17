from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

from sidecar import gateway


def structured_data(structured: dict[str, Any] | None) -> dict[str, Any]:
    assert structured is not None
    return structured


async def wait_for_process_exit(pid: int) -> None:
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        await asyncio.sleep(0.02)
    pytest.fail(f"upstream child process {pid} was not terminated")


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
    monkeypatch.delenv("PI_CODEMCP_AGENT_DIR", raising=False)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path))

    config, oauth, catalog = gateway._runtime_paths()

    assert config == tmp_path / "mcp.json"
    assert oauth == tmp_path / "pi-codemcp" / "oauth"
    assert catalog == tmp_path / "pi-codemcp" / "catalog"


def configure_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    config_path: Path,
) -> None:
    assert config_path == tmp_path / "mcp.json"
    monkeypatch.setenv("PI_CODEMCP_AGENT_DIR", str(tmp_path))


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
        assert exposed == {"search", "get_schema", "execute", "status"}

        initial_status = await client.call_tool("status", {})
        initial_data = structured_data(initial_status.structured_content)
        assert initial_data["tool_count"] == 0
        assert not alpha_pid.exists()
        assert not beta_pid.exists()

        search = await client.call_tool("search", {"query": "save number"})
        search_data = structured_data(search.structured_content)
        assert search_data["total_tool_count"] == 3
        assert search_data["servers"] == [
            {"name": "alpha", "tool_count": 2},
            {"name": "beta", "tool_count": 1},
        ]
        assert search_data["results"][0]["name"] == "beta_save_number"
        assert search_data["results"][0]["call"] == "beta.save_number"
        assert "call_tool" not in search_data["results"][0]["signature"]
        beta_search = await client.call_tool(
            "search", {"query": "number", "server": "beta"}
        )
        beta_search_data = structured_data(beta_search.structured_content)
        assert [item["name"] for item in beta_search_data["results"]] == [
            "beta_save_number"
        ]
        discovery_pids = [int(alpha_pid.read_text()), int(beta_pid.read_text())]
        for pid in discovery_pids:
            await wait_for_process_exit(pid)
        alpha_pid.unlink()
        beta_pid.unlink()

        schema = await client.call_tool(
            "get_schema",
            {"tools": ["alpha_get_number", "beta_save_number"]},
        )
        schema_data = structured_data(schema.structured_content)
        assert len(schema_data["tools"]) == 2
        assert schema_data["tools"][0]["call"] == "alpha.get_number"
        assert "input_schema" not in schema_data["tools"][0]
        assert "output_schema" not in schema_data["tools"][0]
        assert "catalog_fingerprint" not in schema_data
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
        assert execution_data == {
            "ok": True,
            "result": {"identifier": "N-5", "saved": True},
            "calls_made": 2,
        }
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
        counts = {item["name"]: item["tool_count"] for item in status["upstreams"]}
        assert counts == {"alpha": 0, "beta": 1}
        await second.call_tool("search", {"query": "number"})
        assert alpha_pid.exists()
        assert not beta_pid.exists()
        changed_pid = int(alpha_pid.read_text())
    await wait_for_process_exit(changed_pid)
