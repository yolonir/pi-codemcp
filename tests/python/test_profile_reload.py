from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from sidecar.gateway import GatewayRuntime
from sidecar.settings import CodeMcpProfile, load_settings


@pytest.mark.asyncio
async def test_profile_reload_reuses_connections_and_applies_policy_without_discovery(
    tmp_path: Path,
) -> None:
    fixtures = Path(__file__).parents[1] / "fixtures"
    config_path = tmp_path / "mcp.json"
    settings_path = tmp_path / "settings.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    name: {
                        "command": sys.executable,
                        "args": [str(fixtures / "upstream_server.py"), name],
                        "env": {"TEST_PID_FILE": str(tmp_path / f"{name}.pid")},
                    }
                    for name in ["alpha", "beta"]
                }
            }
        )
    )
    settings = load_settings(fixtures / "profile-settings.json")
    settings.profiles["both"] = CodeMcpProfile(servers=["alpha", "beta"])
    settings_path.write_text(settings.model_dump_json(by_alias=True))
    runtime = GatewayRuntime.create(
        config_path, tmp_path / "oauth", tmp_path / "catalog", settings_path
    )
    try:
        first = await runtime.execute(
            'return await alpha.get_number({"seed": 4})', "test:reload"
        )
        assert first.ok
        alpha = runtime.handles["alpha"]
        alpha_client = alpha.client
        alpha_tools = alpha.tools
        assert alpha_client is not None
        assert alpha_tools is not None

        settings.active_profile = "both"
        settings_path.write_text(settings.model_dump_json(by_alias=True))
        await runtime.reload_settings()
        assert runtime.handles["alpha"] is alpha
        assert alpha.client is alpha_client
        assert alpha.tools is alpha_tools
        assert not (tmp_path / "beta.pid").exists()
        assert runtime.handles["beta"].tools is None

        settings.profiles["both"].disabled_tools = {"alpha": ["get_number"]}
        settings_path.write_text(settings.model_dump_json(by_alias=True))
        await runtime.reload_settings()
        blocked = await runtime.execute(
            'return await alpha.get_number({"seed": 4})', "test:reload"
        )
        assert not blocked.ok
        assert blocked.failure_stage == "preflight"
        assert alpha.client is alpha_client
        assert alpha.tools is alpha_tools

        # Invalid settings must not partially replace a working runtime.
        settings_path.write_text('{"activeProfile":"missing"}')
        with pytest.raises(ValidationError, match="Unknown CodeMCP profile"):
            await runtime.reload_settings()
        assert runtime.handles["alpha"] is alpha
        assert runtime.settings.active_profile == "both"
        assert not runtime.settings.tool_enabled("alpha", "get_number")

        settings.active_profile = "offline"
        settings_path.write_text(settings.model_dump_json(by_alias=True))
        execution = asyncio.create_task(
            runtime.execute(
                'return await alpha.slow_number({"delay_seconds": 0.05})', "test:reload"
            )
        )
        await asyncio.sleep(0)
        assert runtime._execute_lock.locked()
        reload_task = asyncio.create_task(runtime.reload_settings())
        await asyncio.sleep(0)
        assert not reload_task.done()
        assert (await execution).ok
        status = await reload_task
        assert all(not server.enabled for server in status.upstreams)
        assert not runtime.handles
        assert not runtime.catalog.tools
        assert not alpha_client.is_connected()
        # A discovery task retaining the old handle cannot reconnect it.
        with pytest.raises(ValueError, match="no longer enabled"):
            await alpha.discover(force=True)
        blocked = await runtime.edit_execute("0.05", "0.01", "test:reload")
        assert not blocked.ok
        assert blocked.failure_stage == "preflight"

        settings.active_profile = "research"
        settings_path.write_text(settings.model_dump_json(by_alias=True))
        await runtime.reload_settings()
        assert runtime.handles["alpha"].tools == alpha_tools
        assert runtime.handles["alpha"].client is None
        assert (
            await runtime.execute(
                'return await alpha.get_number({"seed": 4})', "test:reload"
            )
        ).ok

        settings.active_profile = None
        settings_path.write_text(settings.model_dump_json(by_alias=True))
        await runtime.reload_settings()
        assert set(runtime.handles) == {"alpha", "beta"}
        assert not runtime.settings.tool_enabled("alpha", "get_number")
        assert (
            await runtime.execute(
                'return await beta.save_number({"value": 1})', "test:reload"
            )
        ).ok
    finally:
        await runtime.close()
