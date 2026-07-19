from __future__ import annotations

import pytest

from sidecar import cli


def test_cli_serve_stdio_delegates_to_gateway_main(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_gateway_main() -> None:
        calls.append("serve")

    monkeypatch.setattr(cli.gateway, "main", fake_gateway_main)

    assert cli.main(["serve", "--stdio"]) == 0
    assert calls == ["serve"]


def test_cli_requires_command() -> None:
    with pytest.raises(SystemExit) as error:
        cli.main([])

    assert error.value.code == 2


def test_cli_serve_requires_stdio_flag() -> None:
    with pytest.raises(SystemExit) as error:
        cli.main(["serve"])

    assert error.value.code == 2
