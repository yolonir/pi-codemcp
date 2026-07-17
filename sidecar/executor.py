from __future__ import annotations

import ast
import asyncio
import hashlib
import textwrap
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

import pydantic_monty
from pydantic import BaseModel, ValidationError, model_serializer
from pydantic_core import to_json

from .json_types import JSON_VALUE_ADAPTER, JsonObject, JsonValue

if TYPE_CHECKING:
    from .chains import SavedChainManifest
    from .schemas import ToolCatalog, ToolSpec

RESULT_BYTE_LIMIT = 16 * 1024
SHAPE_FIELD_LIMIT = 20
CHAIN_INPUT_EXTERNAL = "__codemcp_saved_chain_input"


@dataclass(slots=True)
class ExecutionContext:
    catalog: ToolCatalog
    call_tool: ContextToolCall
    settings: ExecutionSettings
    deadline: float
    calls_made: int = 0
    chain_calls: int = 0
    chain_stack: ContextVar[tuple[str, ...]] = field(
        default_factory=lambda: ContextVar("codemcp_chain_stack", default=())
    )

    @property
    def total_calls(self) -> int:
        return self.calls_made + self.chain_calls

    def remaining_seconds(self) -> float:
        return self.deadline - asyncio.get_running_loop().time()


ToolCall = Callable[[str, JsonObject], Awaitable[JsonValue]]
ContextToolCall = Callable[[str, JsonObject, ExecutionContext], Awaitable[JsonValue]]
ExternalFunction = Callable[..., Awaitable[JsonValue]]


class ExecutionResponse(BaseModel):
    ok: bool
    failure_stage: Literal["preflight", "runtime", "timeout", "cancelled", "result"] | None = None
    result: JsonValue = None
    error: str | None = None
    shape: JsonObject | None = None
    calls_made: int = 0
    chain_calls: int = 0

    @model_serializer(mode="plain")
    def serialize_compact(self) -> JsonObject:
        if self.ok:
            response: JsonObject = {
                "ok": True,
                "result": self.result,
                "calls_made": self.calls_made,
            }
        else:
            response = {
                "ok": False,
                "failure_stage": self.failure_stage,
                "error": self.error,
                "calls_made": self.calls_made,
            }
            if self.shape is not None:
                response["shape"] = self.shape
        if self.chain_calls > 0:
            response["chain_calls"] = self.chain_calls
        return response


@dataclass(slots=True)
class ExecutionSettings:
    timeout_seconds: float = 30.0
    max_memory_bytes: int = 100 * 1024 * 1024
    max_calls: int = 50
    tool_timeout_seconds: float = 30.0
    result_byte_limit: int = RESULT_BYTE_LIMIT
    max_chain_depth: int = 16


class MontyExecutor:
    """Type-check and execute one model-authored call graph at a time."""

    def __init__(
        self,
        catalog: ToolCatalog,
        *,
        settings: ExecutionSettings | None = None,
    ) -> None:
        self.catalog = catalog
        self.settings = settings or ExecutionSettings()
        self._execution_lock = asyncio.Lock()
        self._validated_programs: set[str] = set()

    def update_catalog(self, catalog: ToolCatalog) -> None:
        self.catalog = catalog

    async def execute(self, code: str, call_tool: ToolCall) -> ExecutionResponse:
        async def adapted(
            name: str,
            arguments: JsonObject,
            _context: ExecutionContext,
        ) -> JsonValue:
            return await call_tool(name, arguments)

        return await self.execute_graph(code, adapted)

    async def execute_graph(
        self,
        code: str,
        call_tool: ContextToolCall,
    ) -> ExecutionResponse:
        async with self._execution_lock:
            context = self._new_context(self.catalog, call_tool)
            return await self._execute_program(code, context, enforce_result_limit=True)

    async def execute_saved_chain(
        self,
        chain: SavedChainManifest,
        arguments: JsonObject,
        call_tool: ContextToolCall,
    ) -> ExecutionResponse:
        async with self._execution_lock:
            catalog = self.catalog
            spec = self._chain_spec(catalog, chain)
            try:
                validated = catalog.validate_arguments(spec.name, arguments)
            except (TypeError, ValidationError, ValueError) as error:
                return ExecutionResponse(
                    ok=False,
                    failure_stage="preflight",
                    error=f"{chain.call}: invalid arguments: {error}",
                )
            context = self._new_context(catalog, call_tool)
            context.chain_stack.set((chain.name,))
            return await self._execute_program(
                chain.code,
                context,
                input_value=validated,
                input_type=spec.input_type_name,
                output_type=spec.output_type_name,
                output_spec_name=spec.name,
                enforce_result_limit=True,
            )

    async def execute_nested_chain(
        self,
        chain: SavedChainManifest,
        arguments: JsonObject,
        context: ExecutionContext,
    ) -> JsonValue:
        stack = context.chain_stack.get()
        if len(stack) >= context.settings.max_chain_depth:
            path = " -> ".join((*stack, chain.name))
            raise RuntimeError(
                f"Saved chain recursion depth exceeded {context.settings.max_chain_depth}: {path}"
            )
        spec = self._chain_spec(context.catalog, chain)
        token = context.chain_stack.set((*stack, chain.name))
        try:
            response = await self._execute_program(
                chain.code,
                context,
                input_value=arguments,
                input_type=spec.input_type_name,
                output_type=spec.output_type_name,
                output_spec_name=spec.name,
                enforce_result_limit=False,
            )
        finally:
            context.chain_stack.reset(token)
        if not response.ok:
            path = " -> ".join((*stack, chain.name))
            raise RuntimeError(
                f"Saved chain {path} failed at {response.failure_stage}: {response.error}"
            )
        return response.result

    async def validate_saved_chain(
        self,
        code: str,
        catalog: ToolCatalog,
        spec: ToolSpec,
    ) -> None:
        if not code.strip():
            raise ValueError("Saved chain code must not be empty")
        wrapped = _wrap_code(
            code,
            input_type=spec.input_type_name,
            output_type=spec.output_type_name,
            typed=True,
        )
        type_stubs = _chain_type_stubs(catalog, spec.input_type_name)
        async with self._execution_lock:
            await self._type_check(wrapped, catalog, type_stubs)

    def _new_context(
        self,
        catalog: ToolCatalog,
        call_tool: ContextToolCall,
    ) -> ExecutionContext:
        loop = asyncio.get_running_loop()
        return ExecutionContext(
            catalog=catalog,
            call_tool=call_tool,
            settings=self.settings,
            deadline=loop.time() + self.settings.timeout_seconds,
        )

    async def _execute_program(  # noqa: C901, PLR0915
        self,
        code: str,
        context: ExecutionContext,
        *,
        input_value: JsonObject | None = None,
        input_type: str | None = None,
        output_type: str | None = None,
        output_spec_name: str | None = None,
        enforce_result_limit: bool,
    ) -> ExecutionResponse:
        if not code.strip():
            return self._failure(context, "preflight", "Execution code must not be empty")

        has_input = input_value is not None
        typed_code = _wrap_code(
            code,
            input_type=input_type,
            output_type=output_type,
            typed=True,
        )
        type_stubs = (
            _chain_type_stubs(context.catalog, input_type)
            if input_type is not None
            else context.catalog.type_stubs
        )
        try:
            await self._type_check(typed_code, context.catalog, type_stubs)
        except pydantic_monty.MontyTypingError as error:
            return self._failure(
                context,
                "preflight",
                error.display("concise", color=False).strip(),
            )
        except pydantic_monty.MontySyntaxError as error:
            return self._failure(context, "preflight", error.display("type-msg").strip())
        except RuntimeError as error:
            return self._failure(
                context,
                "preflight",
                f"Type-check setup failed: {error}",
            )

        runtime_wrapped = _wrap_code(code, typed=False, has_input=has_input)
        runtime_code = _rewrite_sdk_calls(runtime_wrapped, context.catalog)
        try:
            monty = await pydantic_monty.Monty.acreate(
                runtime_code,
                script_name="codemcp_execute.py",
            )
        except (pydantic_monty.MontySyntaxError, RuntimeError) as error:
            return self._failure(
                context,
                "preflight",
                f"SDK facade compilation failed: {error}",
            )

        async def dispatch_wrapper(name: str, arguments: JsonObject) -> JsonValue:
            catalog = context.catalog
            spec = catalog.tools.get(name)
            if spec is None:
                raise RuntimeError(f"Unknown tool: {name}")
            if not isinstance(arguments, dict):
                raise TypeError("SDK method arguments must be an object")
            if context.total_calls >= context.settings.max_calls:
                raise RuntimeError(
                    f"Call limit exceeded: maximum {context.settings.max_calls} total calls"
                )
            validated = catalog.validate_arguments(name, arguments)
            if spec.kind == "saved_chain":
                context.chain_calls += 1
                return await context.call_tool(name, validated, context)

            context.calls_made += 1
            remaining = context.remaining_seconds()
            if remaining <= 0:
                raise TimeoutError
            timeout = min(context.settings.tool_timeout_seconds, remaining)
            async with asyncio.timeout(timeout):
                return await context.call_tool(name, validated, context)

        external_functions: dict[str, ExternalFunction] = {}
        for spec in context.catalog.tools.values():

            async def sdk_method(
                arguments: JsonObject,
                *,
                _name: str = spec.name,
            ) -> JsonValue:
                return await dispatch_wrapper(_name, arguments)

            external_functions[spec.external_name] = sdk_method

        if input_value is not None:

            async def chain_input() -> JsonValue:  # noqa: RUF029
                return input_value

            external_functions[CHAIN_INPUT_EXTERNAL] = chain_input

        remaining = context.remaining_seconds()
        if remaining <= 0:
            return self._failure(
                context,
                "timeout",
                f"Execution timed out after {context.settings.timeout_seconds:g}s",
            )
        limits: pydantic_monty.ResourceLimits = {
            "max_duration_secs": remaining,
            "max_memory": context.settings.max_memory_bytes,
        }
        try:
            async with asyncio.timeout(remaining + 0.1):
                result = JSON_VALUE_ADAPTER.validate_python(
                    await monty.run_async(
                        external_functions=external_functions,
                        limits=limits,
                    )
                )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return self._failure(
                context,
                "timeout",
                f"Execution timed out after {context.settings.timeout_seconds:g}s",
            )
        except ValidationError as error:
            return self._failure(
                context,
                "result",
                f"Returned value is not JSON-compatible: {error.errors()[0]['msg']}",
            )
        except pydantic_monty.MontyRuntimeError as error:
            message = error.display("type-msg").strip()
            lowered = message.lower()
            stage: Literal["runtime", "timeout"] = (
                "timeout"
                if "duration" in lowered or "timed out" in lowered or "timeout" in lowered
                else "runtime"
            )
            return self._failure(context, stage, message)

        if output_spec_name is not None:
            try:
                result = context.catalog.validate_saved_chain_result(output_spec_name, result)
            except (TypeError, ValidationError, ValueError) as error:
                return self._failure(
                    context,
                    "result",
                    f"Saved chain result violates its output schema: {error}",
                )

        if enforce_result_limit:
            result_bytes = len(to_json(result))
            if result_bytes >= context.settings.result_byte_limit:
                return ExecutionResponse(
                    ok=False,
                    failure_stage="result",
                    error=(
                        f"Returned value is {result_bytes} bytes; reduce it below "
                        f"{context.settings.result_byte_limit} bytes"
                    ),
                    shape=_summarize_shape(result),
                    calls_made=context.calls_made,
                    chain_calls=context.chain_calls,
                )
        return ExecutionResponse(
            ok=True,
            result=result,
            calls_made=context.calls_made,
            chain_calls=context.chain_calls,
        )

    async def _type_check(
        self,
        wrapped_code: str,
        catalog: ToolCatalog,
        type_stubs: str,
    ) -> None:
        key = hashlib.sha256(
            f"{catalog.fingerprint}\0{wrapped_code}\0{type_stubs}".encode()
        ).hexdigest()
        if key in self._validated_programs:
            return
        await pydantic_monty.Monty.acreate(
            wrapped_code,
            script_name="codemcp_execute.py",
            type_check=True,
            type_check_stubs=type_stubs,
        )
        self._validated_programs.add(key)

    @staticmethod
    def _chain_spec(catalog: ToolCatalog, chain: SavedChainManifest) -> ToolSpec:
        spec = catalog.tools.get(chain.public_name)
        if spec is None or spec.kind != "saved_chain":
            raise ValueError(f"Saved chain is not active in the catalog: {chain.name}")
        return spec

    @staticmethod
    def _failure(
        context: ExecutionContext,
        stage: Literal["preflight", "runtime", "timeout", "cancelled", "result"],
        error: str,
    ) -> ExecutionResponse:
        return ExecutionResponse(
            ok=False,
            failure_stage=stage,
            error=error,
            calls_made=context.calls_made,
            chain_calls=context.chain_calls,
        )


def _chain_type_stubs(catalog: ToolCatalog, _input_type: str) -> str:
    return catalog.type_stubs


def _summarize_shape(value: JsonValue) -> JsonObject:
    if not isinstance(value, dict):
        return {"result": _shape_label(value)}
    summary: JsonObject = {
        str(key): _shape_label(item) for key, item in list(value.items())[:SHAPE_FIELD_LIMIT]
    }
    remaining = len(value) - len(summary)
    if remaining > 0:
        summary["<remaining>"] = f"{remaining} more fields"
    return summary


def _shape_label(value: JsonValue) -> str:
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


def _wrap_code(
    code: str,
    *,
    input_type: str | None = None,
    output_type: str | None = None,
    typed: bool,
    has_input: bool | None = None,
) -> str:
    """Allow a natural top-level return and optionally bind typed saved-chain input."""
    normalized = textwrap.dedent(code).strip("\n")
    uses_input = input_type is not None if has_input is None else has_input
    if typed and uses_input:
        if input_type is None or output_type is None:
            raise ValueError("Typed saved-chain code requires input and output types")
        signature = f"input: {input_type}"
        return_annotation = f" -> {output_type}"
    elif uses_input:
        signature = "input"
        return_annotation = ""
    else:
        signature = ""
        return_annotation = ""
    if typed and uses_input:
        invocation = ""
    elif uses_input:
        invocation = f"await __codemcp_main(await {CHAIN_INPUT_EXTERNAL}())\n"
    else:
        invocation = "await __codemcp_main()\n"
    return (
        f"async def __codemcp_main({signature}){return_annotation}:\n"
        f"{textwrap.indent(normalized, '    ')}\n\n"
        f"{invocation}"
    )


def _rewrite_sdk_calls(code: str, catalog: ToolCatalog) -> str:
    tree = ast.parse(code, filename="codemcp_execute.py", mode="exec")

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
