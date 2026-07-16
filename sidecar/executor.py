from __future__ import annotations

import ast
import asyncio
import textwrap
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal

import pydantic_monty
from pydantic import BaseModel, model_serializer
from pydantic_core import to_json

from .schemas import ToolCatalog

RESULT_BYTE_LIMIT = 16 * 1024
SHAPE_FIELD_LIMIT = 20

ToolCall = Callable[[str, dict[str, Any]], Awaitable[Any]]


class ExecutionResponse(BaseModel):
    ok: bool
    failure_stage: Literal[
        "preflight", "runtime", "timeout", "cancelled", "result"
    ] | None = None
    result: Any | None = None
    error: str | None = None
    shape: dict[str, str] | None = None
    calls_made: int = 0

    @model_serializer(mode="plain")
    def serialize_compact(self) -> dict[str, Any]:
        if self.ok:
            return {
                "ok": True,
                "result": self.result,
                "calls_made": self.calls_made,
            }
        response: dict[str, Any] = {
            "ok": False,
            "failure_stage": self.failure_stage,
            "error": self.error,
            "calls_made": self.calls_made,
        }
        if self.shape is not None:
            response["shape"] = self.shape
        return response


@dataclass(slots=True)
class ExecutionSettings:
    timeout_seconds: float = 30.0
    max_memory_bytes: int = 100 * 1024 * 1024
    max_calls: int = 50
    tool_timeout_seconds: float = 30.0
    result_byte_limit: int = RESULT_BYTE_LIMIT


class MontyExecutor:
    """Type-check and execute one model-authored chain at a time."""

    def __init__(
        self,
        catalog: ToolCatalog,
        *,
        settings: ExecutionSettings | None = None,
    ) -> None:
        self.catalog = catalog
        self.settings = settings or ExecutionSettings()
        self._execution_lock = asyncio.Lock()

    def update_catalog(self, catalog: ToolCatalog) -> None:
        self.catalog = catalog

    async def execute(self, code: str, call_tool: ToolCall) -> ExecutionResponse:
        async with self._execution_lock:
            return await self._execute_locked(code, call_tool)

    async def _execute_locked(self, code: str, call_tool: ToolCall) -> ExecutionResponse:
        if not code.strip():
            return ExecutionResponse(
                ok=False,
                failure_stage="preflight",
                error="Execution code must not be empty",
            )

        catalog = self.catalog
        calls_made = 0

        async def dispatch_wrapper(name: str, arguments: dict[str, Any]) -> Any:
            nonlocal calls_made
            if name not in catalog.tools:
                raise RuntimeError(f"Unknown tool: {name}")
            if not isinstance(arguments, dict):
                raise TypeError("SDK method arguments must be an object")
            if calls_made >= self.settings.max_calls:
                raise RuntimeError(
                    f"Call limit exceeded: maximum {self.settings.max_calls} tool calls"
                )
            validated = catalog.validate_arguments(name, arguments)
            calls_made += 1
            async with asyncio.timeout(self.settings.tool_timeout_seconds):
                return await call_tool(name, validated)

        wrapped_code = _wrap_code(code)
        try:
            await pydantic_monty.Monty.acreate(
                wrapped_code,
                script_name="codemode_execute.py",
                type_check=True,
                type_check_stubs=catalog.type_stubs,
            )
        except pydantic_monty.MontyTypingError as error:
            return ExecutionResponse(
                ok=False,
                failure_stage="preflight",
                error=error.display("concise", color=False).strip(),
            )
        except pydantic_monty.MontySyntaxError as error:
            return ExecutionResponse(
                ok=False,
                failure_stage="preflight",
                error=error.display("type-msg").strip(),
            )
        except RuntimeError as error:
            return ExecutionResponse(
                ok=False,
                failure_stage="preflight",
                error=f"Type-check setup failed: {error}",
            )

        runtime_code = _rewrite_sdk_calls(wrapped_code, catalog)
        try:
            monty = await pydantic_monty.Monty.acreate(
                runtime_code,
                script_name="codemode_execute.py",
            )
        except (pydantic_monty.MontySyntaxError, RuntimeError) as error:
            return ExecutionResponse(
                ok=False,
                failure_stage="preflight",
                error=f"SDK facade compilation failed: {error}",
            )

        external_functions: dict[str, Callable[..., Any]] = {}
        for spec in catalog.tools.values():

            async def sdk_method(
                arguments: dict[str, Any],
                *,
                _name: str = spec.name,
            ) -> Any:
                return await dispatch_wrapper(_name, arguments)

            external_functions[spec.external_name] = sdk_method

        limits: pydantic_monty.ResourceLimits = {
            "max_duration_secs": self.settings.timeout_seconds,
            "max_memory": self.settings.max_memory_bytes,
        }
        try:
            async with asyncio.timeout(self.settings.timeout_seconds + 0.5):
                result = await monty.run_async(
                    external_functions=external_functions,
                    limits=limits,
                )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return ExecutionResponse(
                ok=False,
                failure_stage="timeout",
                error=f"Execution timed out after {self.settings.timeout_seconds:g}s",
                calls_made=calls_made,
            )
        except pydantic_monty.MontyRuntimeError as error:
            message = error.display("type-msg").strip()
            lowered = message.lower()
            stage: Literal["runtime", "timeout"] = (
                "timeout"
                if "duration" in lowered or "timed out" in lowered or "timeout" in lowered
                else "runtime"
            )
            return ExecutionResponse(
                ok=False,
                failure_stage=stage,
                error=message,
                calls_made=calls_made,
            )

        result_bytes = len(to_json(result))
        if result_bytes >= self.settings.result_byte_limit:
            return ExecutionResponse(
                ok=False,
                failure_stage="result",
                error=(
                    f"Returned value is {result_bytes} bytes; reduce it below "
                    f"{self.settings.result_byte_limit} bytes"
                ),
                shape=_summarize_shape(result),
                calls_made=calls_made,
            )
        return ExecutionResponse(
            ok=True,
            result=result,
            calls_made=calls_made,
        )


def _summarize_shape(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {"result": _shape_label(value)}
    summary = {
        str(key): _shape_label(item)
        for key, item in list(value.items())[:SHAPE_FIELD_LIMIT]
    }
    remaining = len(value) - len(summary)
    if remaining > 0:
        summary["<remaining>"] = f"{remaining} more fields"
    return summary


def _shape_label(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, (list, tuple)):
        return f"array[{len(value)}]"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return type(value).__name__


def _wrap_code(code: str) -> str:
    """Allow a natural top-level ``return`` while keeping one final result value."""
    normalized = textwrap.dedent(code).strip("\n")
    return (
        "async def __codemode_main():\n"
        f"{textwrap.indent(normalized, '    ')}\n\n"
        "await __codemode_main()\n"
    )


def _rewrite_sdk_calls(code: str, catalog: ToolCatalog) -> str:
    tree = ast.parse(code, filename="codemode_execute.py", mode="exec")

    class FacadeCallRewriter(ast.NodeTransformer):
        def visit_Call(self, node: ast.Call) -> ast.AST:
            self.generic_visit(node)
            function = node.func
            if not isinstance(function, ast.Attribute):
                return node
            if not isinstance(function.value, ast.Name):
                return node
            public_name = catalog.facade_calls.get((function.value.id, function.attr))
            if public_name is None:
                return node
            node.func = ast.copy_location(
                ast.Name(
                    id=catalog.tools[public_name].external_name,
                    ctx=ast.Load(),
                ),
                function,
            )
            return node

    rewritten = FacadeCallRewriter().visit(tree)
    ast.fix_missing_locations(rewritten)
    return ast.unparse(rewritten)
