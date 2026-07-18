from __future__ import annotations

import asyncio

import pytest
from mcp import types as mcp_types
from pydantic import ValidationError

from sidecar.executor import ExecutionResponse, ExecutionSettings, MontyExecutor
from sidecar.json_types import JsonObject, JsonValue
from sidecar.tool_catalog import ToolCatalog


OBJECT = {"type": "object", "properties": {}, "additionalProperties": False}


def test_execution_models_reject_invalid_states_and_limits() -> None:
    with pytest.raises(ValidationError, match="max_calls"):
        ExecutionSettings(max_calls=0)
    with pytest.raises(ValidationError, match="successful execution"):
        ExecutionResponse(ok=True, error="unexpected")
    with pytest.raises(ValidationError, match="requires a failure stage"):
        ExecutionResponse(ok=False)


def tool(
    name: str,
    properties: JsonObject,
    required: list[str],
    output: JsonObject | None,
) -> mcp_types.Tool:
    return mcp_types.Tool(
        name=name,
        inputSchema={
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
        outputSchema=output,
    )


def catalog() -> ToolCatalog:
    return ToolCatalog.from_mcp_tools(
        [
            tool(
                "alpha_get",
                {"id": {"type": "string"}},
                ["id"],
                {
                    "type": "object",
                    "properties": {"value": {"type": "integer"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            ),
            tool("alpha_dynamic", {}, [], None),
            tool(
                "beta_put",
                {"value": {"type": "integer"}},
                ["value"],
                {
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                    "additionalProperties": False,
                },
            ),
            tool(
                "linear_update_issue",
                {
                    "id": {"type": "string"},
                    "priority": {"type": "integer"},
                },
                ["id", "priority"],
                {
                    "type": "object",
                    "properties": {"identifier": {"type": "string"}},
                    "required": ["identifier"],
                },
            ),
        ],
        ["alpha", "beta", "linear"],
    )


@pytest.mark.asyncio
async def test_valid_three_call_cross_server_chain_and_mutation() -> None:
    seen: list[tuple[str, JsonObject]] = []

    async def call(name: str, arguments: JsonObject) -> JsonValue:
        seen.append((name, arguments))
        if name == "alpha_get":
            return {"value": 2}
        if name == "beta_put":
            return {"ok": True}
        return {"identifier": "LIN-1"}

    response = await MontyExecutor(catalog()).execute(
        """
        first = await alpha.get({"id": "one"})
        saved = await beta.put({"value": first["value"]})
        updated = await linear.update_issue({
            "id": "issue-id",
            "priority": first["value"],
        })
        return {"saved": saved["ok"], "issue": updated["identifier"]}
        """,
        call,
    )

    assert response.ok is True
    assert response.result == {"saved": True, "issue": "LIN-1"}
    assert response.calls_made == 3
    assert response.metrics.typecheck_ms >= 0
    assert response.metrics.runtime_ms > 0
    assert response.metrics.serialization_ms >= 0
    assert response.metrics.result_bytes > 0
    assert "metrics" not in response.model_dump(mode="json")
    assert [name for name, _ in seen] == [
        "alpha_get",
        "beta_put",
        "linear_update_issue",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    [
        "return await unknown.tool({})",
        "return await alpha.get({})",
        'return await alpha.get({"id": 1})',
        """
        result = await alpha.get({"id": "x"})
        return result["missing"]
        """,
    ],
)
async def test_preflight_errors_make_zero_calls(code: str) -> None:
    calls = 0

    async def call(_: str, __: JsonObject) -> JsonValue:
        nonlocal calls
        calls += 1
        return {}

    response = await MontyExecutor(catalog()).execute(code, call)

    assert response.ok is False
    assert response.failure_stage == "preflight"
    assert response.calls_made == 0
    assert calls == 0
    assert response.error


@pytest.mark.asyncio
async def test_executor_typechecks_only_referenced_sdk_stubs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[set[str], str | None]] = []
    original = ToolCatalog.type_stubs_for

    def capture(
        self: ToolCatalog,
        public_names: set[str],
        *,
        include: str | None = None,
    ) -> str:
        captured.append((set(public_names), include))
        return original(self, public_names, include=include)

    monkeypatch.setattr(ToolCatalog, "type_stubs_for", capture)

    async def call(_: str, __: JsonObject) -> JsonValue:
        return {"value": 1}

    response = await MontyExecutor(catalog()).execute(
        'return await alpha.get({"id": "one"})',
        call,
    )

    assert response.ok is True
    assert captured == [({"alpha_get"}, None)]


@pytest.mark.asyncio
async def test_untyped_output_must_be_narrowed_before_typed_use() -> None:
    calls = 0

    async def call(_: str, __: JsonObject) -> JsonValue:
        nonlocal calls
        calls += 1
        return {"value": "not-an-integer"}

    response = await MontyExecutor(catalog()).execute(
        """
        dynamic_args = await alpha.dynamic({})
        return await beta.put(dynamic_args)
        """,
        call,
    )

    assert response.ok is False
    assert response.failure_stage == "preflight"
    assert response.calls_made == 0
    assert calls == 0


@pytest.mark.asyncio
async def test_call_limit_stops_before_extra_dispatch() -> None:
    seen: list[str] = []

    async def call(name: str, _: JsonObject) -> JsonValue:
        seen.append(name)
        return {"value": 1}

    executor = MontyExecutor(catalog(), settings=ExecutionSettings(max_calls=2))
    response = await executor.execute(
        """
        await alpha.get({"id": "1"})
        await alpha.get({"id": "2"})
        return await alpha.get({"id": "3"})
        """,
        call,
    )

    assert response.ok is False
    assert response.failure_stage == "runtime"
    assert response.calls_made == 2
    assert seen == ["alpha_get", "alpha_get"]


@pytest.mark.asyncio
async def test_oversized_result_fails_with_shape_without_retry() -> None:
    calls = 0

    async def call(_: str, __: JsonObject) -> JsonValue:
        nonlocal calls
        calls += 1
        return {"summary": {"title": "large"}, "panels": ["x" * 100]}

    response = await MontyExecutor(
        catalog(), settings=ExecutionSettings(result_byte_limit=64)
    ).execute("return await alpha.dynamic({})", call)

    assert response.ok is False
    assert response.failure_stage == "result"
    assert response.error and "reduce it below 64 bytes" in response.error
    shape = response.shape
    assert shape is not None
    assert shape["type"] == "object"
    serialized_bytes = shape["serialized_bytes"]
    assert isinstance(serialized_bytes, int) and serialized_bytes > 64
    assert shape["cardinality"] == 2
    assert shape["shape"] == {
        "summary": {"title": "string"},
        "panels": {"items": ["string"], "count": 1},
    }
    field_sizes = shape["field_sizes"]
    assert isinstance(field_sizes, list) and field_sizes
    first_field = field_sizes[0]
    assert isinstance(first_field, dict) and first_field["path"] == "$.panels"
    samples = shape["samples"]
    assert isinstance(samples, list) and samples
    first_sample = samples[0]
    assert isinstance(first_sample, dict)
    panels = first_sample["panels"]
    assert isinstance(panels, list) and panels[0] == "x" * 100
    assert response.calls_made == calls == 1


@pytest.mark.asyncio
async def test_inspect_json_reports_bounded_runtime_shape_and_samples() -> None:
    async def call(_: str, __: JsonObject) -> JsonValue:
        return [
            {"id": 1, "message": "x" * 250, "labels": {"service": "api"}},
            {"id": 2, "message": "ok", "labels": {"service": "worker"}},
            {"id": 3, "message": "ignored", "labels": {"service": "api"}},
        ]

    response = await MontyExecutor(catalog()).execute(
        """
        value = await alpha.dynamic({})
        return inspect_json(value, samples=2, max_depth=3)
        """,
        call,
    )

    assert response.ok is True
    assert isinstance(response.result, dict)
    assert response.result["type"] == "array[3]"
    assert response.result["cardinality"] == 3
    assert response.result["common_keys"] == ["id", "labels", "message"]
    assert response.result["scalar_types"] == {
        "$[].id": ["integer"],
        "$[].labels.service": ["string"],
        "$[].message": ["string"],
    }
    result_samples = response.result["samples"]
    assert isinstance(result_samples, list) and len(result_samples) == 2
    result_first = result_samples[0]
    assert isinstance(result_first, dict)
    message = result_first["message"]
    assert isinstance(message, str) and message.endswith("…")
    assert response.calls_made == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code, message",
    [
        ("return inspect_json([], samples=0)", "samples must be from"),
        ("return inspect_json([], samples=4)", "samples must be from"),
        ("return inspect_json([], max_depth=0)", "max_depth must be from"),
    ],
)
async def test_inspect_json_rejects_unbounded_options(code: str, message: str) -> None:
    async def call(_: str, __: JsonObject) -> JsonValue:
        raise AssertionError("no MCP tool call expected")

    response = await MontyExecutor(catalog()).execute(code, call)
    assert response.ok is False
    assert response.failure_stage == "runtime"
    assert response.error and message in response.error
    assert response.calls_made == 0


@pytest.mark.asyncio
async def test_timeout_stops_infinite_sandbox_loop() -> None:
    executor = MontyExecutor(
        catalog(),
        settings=ExecutionSettings(timeout_seconds=0.01),
    )

    async def call(_: str, __: JsonObject) -> JsonValue:
        raise AssertionError("no tool call expected")

    response = await executor.execute("while True:\n    pass", call)
    assert response.ok is False
    assert response.failure_stage == "timeout"
    assert response.calls_made == 0


@pytest.mark.asyncio
async def test_cancellation_propagates_without_retry() -> None:
    started = asyncio.Event()
    blocker = asyncio.Event()
    calls = 0

    async def call(_: str, __: JsonObject) -> JsonValue:
        nonlocal calls
        calls += 1
        started.set()
        await blocker.wait()
        return {"value": 1}

    task = asyncio.create_task(
        MontyExecutor(catalog()).execute(
            'return await alpha.get({"id": "x"})',
            call,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == 1


@pytest.mark.asyncio
async def test_loops_conditions_and_per_session_serialization() -> None:
    active = 0
    max_active = 0

    async def call(_: str, arguments: JsonObject) -> JsonValue:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        raw_id = arguments["id"]
        assert isinstance(raw_id, str)
        return {"value": int(raw_id)}

    executor = MontyExecutor(catalog())
    code = """
    values = []
    for item in ["1", "2", "3"]:
        result = await alpha.get({"id": item})
        if result["value"] > 1:
            values.append(result["value"])
    return values
    """
    first, second = await asyncio.gather(
        executor.execute(code, call),
        executor.execute(code, call),
    )

    assert first.result == [2, 3]
    assert second.result == [2, 3]
    assert max_active == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    [
        "import os\nreturn os.environ",
        'return open("/etc/passwd").read()',
        'return __import__("subprocess")',
        "import socket\nreturn socket.socket()",
    ],
)
async def test_sandbox_denies_host_capabilities(code: str) -> None:
    calls = 0

    async def call(_: str, __: JsonObject) -> JsonValue:
        nonlocal calls
        calls += 1
        return {}

    response = await MontyExecutor(catalog()).execute(code, call)
    assert response.ok is False
    assert response.calls_made == 0
    assert calls == 0


@pytest.mark.asyncio
async def test_normalize_result_parses_json_object_and_array_text() -> None:
    current = catalog()
    object_result = mcp_types.CallToolResult(
        content=[
            mcp_types.TextContent(
                type="text",
                text='{"columns":["1"],"rows":[[1]]}',
            )
        ],
        isError=False,
    )
    assert current.normalize_result("alpha_dynamic", object_result) == {
        "columns": ["1"],
        "rows": [[1]],
    }

    array_result = mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text='[{"id":1}]')],
        isError=False,
    )
    assert current.normalize_result("alpha_dynamic", array_result) == [{"id": 1}]

    for text, expected in (
        ("null", None),
        ("true", True),
        ("false", False),
        ("plain text", "plain text"),
        ("{not json}", "{not json}"),
        ('"json scalar"', '"json scalar"'),
        ("00123", "00123"),
    ):
        result = mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text=text)],
            isError=False,
        )
        assert current.normalize_result("alpha_dynamic", result) == expected

    clickhouse_catalog = ToolCatalog.from_server_tools(
        {
            "clickhouse": [
                tool(
                    "run_query",
                    {"query": {"type": "string"}},
                    ["query"],
                    {
                        "type": "object",
                        "properties": {"result": {"type": "string"}},
                        "required": ["result"],
                        "x-fastmcp-wrap-result": True,
                    },
                )
            ]
        }
    )
    wrapped = mcp_types.CallToolResult(
        content=[],
        structuredContent={"result": '{"columns":["1"],"rows":[[1]]}'},
        isError=False,
    )
    assert clickhouse_catalog.normalize_result("clickhouse_run_query", wrapped) == {
        "columns": ["1"],
        "rows": [[1]],
    }

    wrapped_null = mcp_types.CallToolResult(
        content=[],
        structuredContent={"result": "null"},
        isError=False,
    )
    assert (
        clickhouse_catalog.normalize_result("clickhouse_run_query", wrapped_null)
        is None
    )

    declared = mcp_types.CallToolResult(
        content=[],
        structuredContent={"value": "null"},
        isError=False,
    )
    declared_catalog = ToolCatalog.from_server_tools(
        {
            "example": [
                tool(
                    "read",
                    {},
                    [],
                    {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                        "required": ["value"],
                    },
                )
            ]
        }
    )
    assert declared_catalog.normalize_result("example_read", declared) == {
        "value": None
    }


@pytest.mark.asyncio
async def test_normalized_json_string_output_is_usable_through_sdk_facade() -> None:
    sdk_catalog = ToolCatalog.from_server_tools(
        {
            "clickhouse": [
                tool(
                    "run_query",
                    {"query": {"type": "string"}},
                    ["query"],
                    {
                        "type": "object",
                        "properties": {"result": {"type": "string"}},
                        "required": ["result"],
                        "x-fastmcp-wrap-result": True,
                    },
                )
            ]
        }
    )

    async def call(_: str, __: JsonObject) -> JsonValue:
        return {"columns": ["1"], "rows": [[1]]}

    response = await MontyExecutor(sdk_catalog).execute(
        """
        result = await clickhouse.run_query({"query": "SELECT 1"})
        if not isinstance(result, dict):
            return None
        rows = result.get("rows")
        if not isinstance(rows, list) or not rows:
            return None
        first_row = rows[0]
        if not isinstance(first_row, list) or not first_row:
            return None
        return first_row[0]
        """,
        call,
    )
    assert response.ok is True
    assert response.result == 1


@pytest.mark.asyncio
async def test_normalize_result_validates_declared_output() -> None:
    current = catalog()
    result = mcp_types.CallToolResult(
        content=[],
        structuredContent={"value": 4},
        isError=False,
    )
    assert current.normalize_result("alpha_get", result) == {"value": 4}

    bad = mcp_types.CallToolResult(
        content=[],
        structuredContent={"value": "bad"},
        isError=False,
    )
    with pytest.raises(Exception):
        current.normalize_result("alpha_get", bad)
