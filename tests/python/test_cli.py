from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from sidecar import cli, gateway

ARGUMENT_ERROR = 2


def write_config(path: Path, value: dict[str, Any] | None = None) -> None:
    path.write_text(json.dumps(value or {"mcpServers": {}}), encoding="utf-8")


def run_json_cli(
    capsys: pytest.CaptureFixture[str],
    args: list[str],
) -> tuple[int, dict[str, Any]]:
    code = cli.main(args)
    captured = capsys.readouterr()
    assert captured.err == ""
    parsed = json.loads(captured.out)
    assert isinstance(parsed, dict)
    return code, parsed


def test_cli_serve_stdio_delegates_to_gateway_main(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, tuple[Path, Path, Path, Path, Path, Path | None]]] = []

    def fake_gateway_main() -> None:
        calls.append(("serve", gateway._runtime_paths()))

    monkeypatch.setattr(gateway, "main", fake_gateway_main)

    assert cli.main(["serve", "--stdio", "--agent-dir", str(tmp_path)]) == 0
    assert calls == [
        (
            "serve",
            (
                tmp_path / "mcp.json",
                tmp_path / "pi-codemcp" / "oauth",
                tmp_path / "pi-codemcp" / "catalog",
                tmp_path / "pi-codemcp" / "settings.json",
                tmp_path / "pi-codemcp" / "chains",
                None,
            ),
        )
    ]
    assert gateway._runtime_state.paths is None


def test_cli_requires_command() -> None:
    with pytest.raises(SystemExit) as error:
        cli.main([])

    assert error.value.code == ARGUMENT_ERROR


def test_cli_serve_requires_stdio_flag() -> None:
    with pytest.raises(SystemExit) as error:
        cli.main(["serve"])

    assert error.value.code == ARGUMENT_ERROR


def test_cli_status_uses_explicit_agent_dir_without_discovery(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_config(
        tmp_path / "mcp.json",
        {"mcpServers": {"placeholder": {"command": "command-that-must-not-run"}}},
    )

    code, payload = run_json_cli(capsys, ["status", "--agent-dir", str(tmp_path)])

    assert code == 0
    assert payload["connected"] is True
    assert payload["config_path"] == str(tmp_path / "mcp.json")
    assert payload["tool_count"] == 0
    assert payload["upstreams"] == [
        {
            "name": "placeholder",
            "transport": "stdio",
            "enabled": True,
            "connected": False,
            "discovered": False,
            "auth": None,
            "tool_count": 0,
            "total_tool_count": 0,
            "tools": [],
        }
    ]


def test_cli_execute_and_saved_chain_commands_are_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    agent_dir = tmp_path / "agent"
    project_chains = tmp_path / "project" / ".pi" / "pi-codemcp" / "chains"
    agent_dir.mkdir()
    write_config(agent_dir / "mcp.json")
    code_file = tmp_path / "plan.py"
    code_file.write_text('return {"value": 7}\n', encoding="utf-8")

    code, executed = run_json_cli(
        capsys,
        ["execute", "--agent-dir", str(agent_dir), "--code-file", str(code_file)],
    )

    assert code == 0
    assert executed["ok"] is True
    assert executed["result"] == {"value": 7}
    assert executed["calls_made"] == 0
    assert set(executed["timings"]) == {
        "typecheck_ms",
        "execution_ms",
        "serialization_ms",
    }

    input_schema = {
        "type": "object",
        "properties": {"value": {"type": "integer"}},
        "required": ["value"],
        "additionalProperties": False,
    }
    output_schema = {
        "type": "object",
        "properties": {"value": {"type": "integer"}},
        "required": ["value"],
        "additionalProperties": False,
    }
    chain_code = 'return {"value": input["value"]}\n'

    code, saved = run_json_cli(
        capsys,
        [
            "chain",
            "save",
            "echo_value",
            "--agent-dir",
            str(agent_dir),
            "--project-chains-dir",
            str(project_chains),
            "--description",
            "Echo one integer value.",
            "--code",
            chain_code,
            "--input-schema",
            json.dumps(input_schema),
            "--output-schema",
            json.dumps(output_schema),
        ],
    )

    assert code == 0
    assert saved["created"] is True
    assert saved["chain"]["chain"]["name"] == "echo_value"
    assert (project_chains / "echo_value.json").is_file()

    code, listed = run_json_cli(
        capsys,
        [
            "chain",
            "list",
            "--agent-dir",
            str(agent_dir),
            "--project-chains-dir",
            str(project_chains),
        ],
    )

    assert code == 0
    assert listed["chains"][0]["chain"]["name"] == "echo_value"

    code, chain_result = run_json_cli(
        capsys,
        [
            "chain",
            "run",
            "echo_value",
            "--agent-dir",
            str(agent_dir),
            "--project-chains-dir",
            str(project_chains),
            "--input",
            '{"value": 9}',
        ],
    )

    assert code == 0
    assert chain_result["ok"] is True
    assert chain_result["result"] == {"value": 9}
    assert chain_result["calls_made"] == 0
    assert set(chain_result["timings"]) == {
        "typecheck_ms",
        "execution_ms",
        "serialization_ms",
    }

    code, revalidated = run_json_cli(
        capsys,
        [
            "chain",
            "revalidate",
            "echo_value",
            "--scope",
            "project",
            "--agent-dir",
            str(agent_dir),
            "--project-chains-dir",
            str(project_chains),
        ],
    )

    assert code == 0
    assert revalidated["status"] == "ready"

    code, deleted = run_json_cli(
        capsys,
        [
            "chain",
            "delete",
            "echo_value",
            "--scope",
            "project",
            "--agent-dir",
            str(agent_dir),
            "--project-chains-dir",
            str(project_chains),
        ],
    )

    assert code == 0
    assert deleted == {"chains": []}
    assert not (project_chains / "echo_value.json").exists()


def test_cli_search_discovers_fixture_server(
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    agent_dir = tmp_path / "agent"
    pid_path = tmp_path / "alpha.pid"
    fixture = Path(__file__).parents[2] / "tests" / "fixtures" / "upstream_server.py"
    agent_dir.mkdir()
    write_config(
        agent_dir / "mcp.json",
        {
            "mcpServers": {
                "alpha": {
                    "command": sys.executable,
                    "args": [str(fixture), "alpha"],
                    "env": {"TEST_PID_FILE": str(pid_path)},
                }
            }
        },
    )

    code, payload = run_json_cli(
        capfd,
        ["search", "number", "--agent-dir", str(agent_dir), "--limit", "1"],
    )

    assert code == 0
    assert payload["total_tool_count"] == 2
    assert payload["results"][0]["call"] == "alpha.get_number"
    assert pid_path.is_file()


def test_cli_doctor_reports_path_health(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_config(tmp_path / "mcp.json")

    code, payload = run_json_cli(capsys, ["doctor", "--agent-dir", str(tmp_path)])

    assert code == 0
    assert payload["ok"] is True
    assert payload["paths"]["config_path"] == str(tmp_path / "mcp.json")
    assert {check["name"]: check["ok"] for check in payload["checks"]} == {
        "mcp_config": True,
        "settings": True,
        "oauth_dir": True,
        "catalog_dir": True,
        "global_chains_dir": True,
        "project_chains_dir": True,
    }
