from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from sidecar.gateway import GatewayRuntime
from sidecar.settings import CodeMcpSettings, load_settings

FIXTURE = Path(__file__).parents[1] / "fixtures" / "profile-settings.json"


def test_profile_policy_matches_the_shared_settings_contract() -> None:
    settings = load_settings(FIXTURE)
    assert settings.enabled_servers == {"alpha"}
    assert settings.tool_enabled("alpha", "get_number")
    assert not settings.tool_enabled("alpha", "slow_number")
    assert not settings.tool_enabled("beta", "save_number")
    base = CodeMcpSettings(
        disabled_tools=settings.disabled_tools, profiles=settings.profiles
    )
    assert base.enabled_servers is None
    assert not base.tool_enabled("alpha", "get_number")
    assert base.tool_enabled("alpha", "slow_number")
    offline = CodeMcpSettings(profiles=settings.profiles, active_profile="offline")
    assert offline.enabled_servers == set()
    assert not offline.tool_enabled("alpha", "get_number")


@pytest.mark.parametrize(
    "value",
    [
        {"activeProfile": "missing"},
        {"profiles": {"bad": {"servers": "alpha"}}},
        {"profiles": {"bad": {"servers": [1]}}},
        {"profiles": {"bad": {"servers": [""]}}},
        {"profiles": {"bad": {"servers": [], "disabledTools": {"alpha": [1]}}}},
        {"profiles": {"bad": {"servers": [], "url": "https://example.com"}}},
        {"profiles": {" ": {"servers": []}}},
    ],
)
def test_profiles_reject_malformed_policies(value: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        CodeMcpSettings.model_validate(value)


@pytest.mark.asyncio
async def test_profiles_enforce_server_and_tool_selection_in_discovery_and_execution(
    tmp_path: Path,
) -> None:
    upstream = FIXTURE.parent / "upstream_server.py"
    config_path = tmp_path / "mcp.json"
    settings_path = tmp_path / "settings.json"
    config = json.dumps(
        {
            "mcpServers": {
                "alpha": {
                    "command": sys.executable,
                    "args": [str(upstream), "alpha"],
                    "disabled": True,
                },
                "beta": {
                    "command": sys.executable,
                    "args": [str(upstream), "beta"],
                    "env": {"TEST_PID_FILE": str(tmp_path / "beta.pid")},
                },
            }
        }
    )
    config_path.write_text(config)
    settings = load_settings(FIXTURE)

    for profile in ["research", "writing", "offline", None]:
        settings.active_profile = profile
        settings_path.write_text(settings.model_dump_json(by_alias=True))
        runtime = GatewayRuntime.create(
            config_path, tmp_path / "oauth", tmp_path / "catalog", settings_path
        )
        try:
            status = runtime.status()
            enabled = {server.name for server in status.upstreams if server.enabled}
            assert enabled == (
                {"beta"}
                if profile in {"writing", None}
                else {"alpha"}
                if profile == "research"
                else set()
            )
            await runtime.search(mode="inventory", trace_id="test:profiles")
            if profile == "research":
                allowed = await runtime.execute(
                    'return await alpha.get_number({"seed": 4})', "test:profiles"
                )
                assert allowed.ok
                assert allowed.result == {"value": 5}
                for code in [
                    'return await alpha.slow_number({"delay_seconds": 0.01})',
                    'return await beta.save_number({"value": 5})',
                ]:
                    blocked = await runtime.execute(code, "test:profiles")
                    assert not blocked.ok
                    assert blocked.failure_stage == "preflight"
                assert not (tmp_path / "beta.pid").exists()
            elif profile == "writing":
                blocked = await runtime.execute(
                    'return await beta.save_number({"value": 5})', "test:profiles"
                )
                assert not blocked.ok
                assert blocked.failure_stage == "preflight"
            elif profile == "offline":
                assert not runtime.catalog.tools
            else:
                allowed = await runtime.execute(
                    'return await beta.save_number({"value": 5})', "test:profiles"
                )
                assert allowed.ok
            assert config_path.read_text() == config
        finally:
            await runtime.close()
