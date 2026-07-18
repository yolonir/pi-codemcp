from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from sidecar.settings import CodeMcpSettings, load_settings


def test_settings_load_defaults_and_camel_case_overrides(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    assert load_settings(path) == CodeMcpSettings()

    path.write_text(
        json.dumps(
            {
                "backgroundWarmup": False,
                "cacheTtlHours": 6,
                "executionTimeoutSeconds": 60,
                "toolTimeoutSeconds": 10,
                "maxCalls": 25,
                "resultLimitKiB": 32,
                "outputLimitKiB": 100,
                "outputLineLimit": 5000,
                "disabledTools": {"linear": ["delete_issue"]},
            }
        )
    )
    settings = load_settings(path)

    assert settings.version == 2
    assert not hasattr(settings, "output_line_limit")
    assert settings.background_warmup is False
    assert settings.cache_ttl_seconds == 6 * 60 * 60
    assert settings.execution_settings().timeout_seconds == 60
    assert settings.execution_settings().result_byte_limit == 32 * 1024
    assert settings.tool_enabled("linear", "delete_issue") is False
    assert settings.tool_enabled("linear", "list_issues") is True


def test_settings_reject_unknown_or_unsafe_values(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"bootstrapTimeout": 1}))
    with pytest.raises(ValidationError, match="bootstrapTimeout"):
        load_settings(path)

    path.write_text(json.dumps({"maxCalls": 0}))
    with pytest.raises(ValidationError, match="maxCalls"):
        load_settings(path)

    path.write_text(json.dumps({"maxCalls": "1"}))
    with pytest.raises(ValidationError, match="maxCalls"):
        load_settings(path)

    path.write_text(json.dumps({"version": 2, "outputLineLimit": 500}))
    with pytest.raises(ValidationError, match="outputLineLimit"):
        load_settings(path)
