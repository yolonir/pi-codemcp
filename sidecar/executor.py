from __future__ import annotations

import ast
import asyncio
import hashlib
import heapq
import textwrap
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from itertools import islice
from typing import TYPE_CHECKING, Literal, Self, assert_never, override

import pydantic_monty
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_serializer,
    model_validator,
)
from pydantic_core import to_json

from .json_types import JSON_VALUE_ADAPTER, JsonObject, JsonValue
from .tool_catalog import referenced_calls, schema_path_summary

if TYPE_CHECKING:
    from .chains import SavedChainManifest
    from .tool_catalog import ToolCatalog, ToolSpec

RESULT_BYTE_LIMIT = 16 * 1024
SHAPE_FIELD_LIMIT = 20
INSPECT_SAMPLE_LIMIT = 3
INSPECT_DEPTH_LIMIT = 6
INSPECT_COLLECTION_LIMIT = 10
INSPECT_STRING_LIMIT = 200
INSPECT_KEY_LIMIT = 120
INSPECT_BYTE_LIMIT = 8 * 1024
CHAIN_INPUT_EXTERNAL = "__codemcp_saved_chain_input"
INSPECT_JSON_EXTERNAL = "__codemcp_inspect_json"


class ExecutionMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    typecheck_ms: float = Field(default=0.0, ge=0)
    runtime_ms: float = Field(default=0.0, ge=0)
    serialization_ms: float = Field(default=0.0, ge=0)
    result_bytes: int = Field(default=0, ge=0)


class ExecutionContext:
    def __init__(
        self,
        catalog: ToolCatalog,
        call_tool: ContextToolCall,
        settings: ExecutionSettings,
        deadline: float,
    ) -> None:
        self.catalog = catalog
        self.call_tool = call_tool
        self.settings = settings
        self.deadline = deadline
        self.calls_made = 0
        self.chain_calls = 0
        self.metrics = ExecutionMetrics()
        self.chain_stack: ContextVar[tuple[str, ...]] = ContextVar(
            "codemcp_chain_stack",
            default=(),
        )

    @property
    def total_calls(self) -> int:
        return self.calls_made + self.chain_calls

    def remaining_seconds(self) -> float:
        return self.deadline - asyncio.get_running_loop().time()


ToolCall = Callable[[str, JsonObject], Awaitable[JsonValue]]
ContextToolCall = Callable[[str, JsonObject, ExecutionContext], Awaitable[JsonValue]]
ExternalFunction = Callable[..., Awaitable[JsonValue]]


type FailureStage = Literal["preflight", "runtime", "timeout", "cancelled", "result"]


class ExecutionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    ok: bool
    failure_stage: FailureStage | None = None
    result: JsonValue = None
    error: str | None = None
    shape: JsonObject | None = None
    calls_made: int = Field(default=0, ge=0)
    chain_calls: int = Field(default=0, ge=0)
    metrics: ExecutionMetrics = Field(default_factory=ExecutionMetrics, exclude=True)

    @classmethod
    def success(
        cls,
        result: JsonValue,
        *,
        calls_made: int = 0,
        chain_calls: int = 0,
    ) -> Self:
        return cls(
            ok=True,
            result=result,
            calls_made=calls_made,
            chain_calls=chain_calls,
        )

    @classmethod
    def failure(
        cls,
        *,
        failure_stage: FailureStage,
        error: str,
        calls_made: int = 0,
        chain_calls: int = 0,
        shape: JsonObject | None = None,
    ) -> Self:
        return cls(
            ok=False,
            failure_stage=failure_stage,
            error=error,
            calls_made=calls_made,
            chain_calls=chain_calls,
            shape=shape,
        )

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if self.ok:
            if self.failure_stage is not None or self.error is not None or self.shape is not None:
                raise ValueError("successful execution cannot contain failure details")
        elif self.failure_stage is None or self.error is None:
            raise ValueError("failed execution requires a failure stage and error")
        elif self.result is not None:
            raise ValueError("failed execution cannot contain a result")
        return self

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
        timings: JsonObject = {
            "typecheck_ms": round(self.metrics.typecheck_ms, 3),
            "execution_ms": round(self.metrics.runtime_ms, 3),
            "serialization_ms": round(self.metrics.serialization_ms, 3),
        }
        response["timings"] = timings
        return response


class ExecutionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    timeout_seconds: float = Field(default=30.0, gt=0)
    max_memory_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    max_calls: int = Field(default=50, gt=0)
    tool_timeout_seconds: float = Field(default=30.0, gt=0)
    result_byte_limit: int = Field(default=RESULT_BYTE_LIMIT, gt=0)
    max_chain_depth: int = Field(default=16, gt=0)


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
                return ExecutionResponse.failure(
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
        referenced = referenced_calls(code, catalog.facade_calls)
        type_stubs = catalog.type_stubs_for(referenced, include=spec.name)
        async with self._execution_lock:
            await self._type_check(wrapped, catalog, type_stubs)
            await self._compile_runtime(code, catalog, has_input=True)

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

    async def _execute_program(  # ruff:ignore[complex-structure, too-many-statements]
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
        referenced = referenced_calls(code, context.catalog.facade_calls)
        type_stubs = context.catalog.type_stubs_for(
            referenced,
            include=output_spec_name,
        )
        typecheck_started = time.perf_counter()
        try:
            await self._type_check(typed_code, context.catalog, type_stubs)
        except pydantic_monty.MontyTypingError as error:
            context.metrics.typecheck_ms += _elapsed_ms(typecheck_started)
            return self._failure(
                context,
                "preflight",
                error.display("concise", color=False).strip(),
            )
        except pydantic_monty.MontySyntaxError as error:
            context.metrics.typecheck_ms += _elapsed_ms(typecheck_started)
            return self._failure(context, "preflight", error.display("type-msg").strip())
        except pydantic_monty.MontyRuntimeError as error:
            context.metrics.typecheck_ms += _elapsed_ms(typecheck_started)
            return self._failure(context, "preflight", error.display("type-msg").strip())
        except RuntimeError as error:
            context.metrics.typecheck_ms += _elapsed_ms(typecheck_started)
            return self._failure(
                context,
                "preflight",
                f"Type-check setup failed: {error}",
            )
        context.metrics.typecheck_ms += _elapsed_ms(typecheck_started)

        try:
            monty = await self._compile_runtime(
                code,
                context.catalog,
                has_input=has_input,
            )
        except (
            pydantic_monty.MontySyntaxError,
            pydantic_monty.MontyRuntimeError,
            RuntimeError,
            NotImplementedError,
        ) as error:
            return self._failure(
                context,
                "preflight",
                f"Sandbox compilation failed: {error}",
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

        async def inspect_json_external(
            value: JsonValue,
            *,
            samples: JsonValue = 2,
            max_depth: JsonValue = 3,
        ) -> JsonValue:
            await asyncio.sleep(0)
            if not isinstance(samples, int) or isinstance(samples, bool):
                raise TypeError("inspect_json samples must be an integer")
            if not isinstance(max_depth, int) or isinstance(max_depth, bool):
                raise TypeError("inspect_json max_depth must be an integer")
            if not 1 <= samples <= INSPECT_SAMPLE_LIMIT:
                raise ValueError(f"inspect_json samples must be from 1 to {INSPECT_SAMPLE_LIMIT}")
            if not 1 <= max_depth <= INSPECT_DEPTH_LIMIT:
                raise ValueError(f"inspect_json max_depth must be from 1 to {INSPECT_DEPTH_LIMIT}")
            return _inspect_json(
                value,
                samples=samples,
                max_depth=max_depth,
                byte_limit=_inspection_byte_limit(context.settings.result_byte_limit),
            )

        external_functions[INSPECT_JSON_EXTERNAL] = inspect_json_external
        for spec in context.catalog.tools.values():

            async def sdk_method(
                arguments: JsonObject,
                *,
                _name: str = spec.name,
            ) -> JsonValue:
                return await dispatch_wrapper(_name, arguments)

            external_functions[spec.external_name] = sdk_method

        if input_value is not None:

            async def chain_input() -> JsonValue:  # ruff:ignore[unused-async]
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
        runtime_started = time.perf_counter()
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
            context.metrics.runtime_ms += _elapsed_ms(runtime_started)
            return self._failure(
                context,
                "timeout",
                f"Execution timed out after {context.settings.timeout_seconds:g}s",
            )
        except ValidationError as error:
            context.metrics.runtime_ms += _elapsed_ms(runtime_started)
            return self._failure(
                context,
                "result",
                f"Returned value is not JSON-compatible: {error.errors()[0]['msg']}",
            )
        except pydantic_monty.MontyRuntimeError as error:
            context.metrics.runtime_ms += _elapsed_ms(runtime_started)
            message = error.display("type-msg").strip()
            lowered = message.lower()
            stage: Literal["runtime", "timeout"] = (
                "timeout"
                if "duration" in lowered or "timed out" in lowered or "timeout" in lowered
                else "runtime"
            )
            return self._failure(context, stage, message)
        context.metrics.runtime_ms += _elapsed_ms(runtime_started)

        if output_spec_name is not None:
            try:
                result = context.catalog.validate_saved_chain_result(output_spec_name, result)
            except (TypeError, ValidationError, ValueError) as error:
                spec = context.catalog.tools[output_spec_name]
                return self._failure(
                    context,
                    "result",
                    _saved_chain_result_error(error, spec.output_schema or {}),
                )

        serialization_started = time.perf_counter()
        result_bytes = len(to_json(result))
        context.metrics.serialization_ms += _elapsed_ms(serialization_started)
        context.metrics.result_bytes = result_bytes
        if enforce_result_limit and result_bytes >= context.settings.result_byte_limit:
            response = ExecutionResponse.failure(
                failure_stage="result",
                error=(
                    f"Returned value is {result_bytes} bytes; reduce it below "
                    f"{context.settings.result_byte_limit} bytes"
                ),
                shape=_inspect_json(
                    result,
                    samples=2,
                    max_depth=3,
                    byte_limit=_inspection_byte_limit(context.settings.result_byte_limit),
                ),
                calls_made=context.calls_made,
                chain_calls=context.chain_calls,
            )
            response.metrics = context.metrics.model_copy()
            return response
        response = ExecutionResponse.success(
            result=result,
            calls_made=context.calls_made,
            chain_calls=context.chain_calls,
        )
        response.metrics = context.metrics.model_copy()
        return response

    @staticmethod
    async def _compile_runtime(
        code: str,
        catalog: ToolCatalog,
        *,
        has_input: bool,
    ) -> pydantic_monty.Monty:
        runtime_wrapped = _wrap_code(code, typed=False, has_input=has_input)
        runtime_code = _rewrite_sdk_calls(runtime_wrapped, catalog)
        return await pydantic_monty.Monty.acreate(
            runtime_code,
            script_name="codemcp_execute.py",
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
        response = ExecutionResponse.failure(
            failure_stage=stage,
            error=error,
            calls_made=context.calls_made,
            chain_calls=context.chain_calls,
        )
        response.metrics = context.metrics.model_copy()
        return response


def _saved_chain_result_error(
    error: TypeError | ValidationError | ValueError,
    output_schema: JsonObject,
) -> str:
    expected = "\n".join(f"  - {path}" for path in schema_path_summary(output_schema))
    if isinstance(error, ValidationError):
        actual_lines = []
        for item in error.errors()[:SHAPE_FIELD_LIMIT]:
            location = "$" + "".join(
                f"[{part}]" if isinstance(part, int) else f".{part}" for part in item["loc"]
            )
            actual_lines.append(
                f"  - {location}: {item['msg']} (actual {type(item.get('input')).__name__})"
            )
        actual = "\n".join(actual_lines)
    else:
        actual = f"  - $: {error}"
    return (
        "Saved chain result violates outputSchema.\n"
        f"Expected output paths:\n{expected}\n"
        f"Actual result paths:\n{actual}"
    )


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1_000


def _inspection_byte_limit(result_byte_limit: int) -> int:
    return max(256, min(INSPECT_BYTE_LIMIT, result_byte_limit * 3 // 4))


def _inspect_json(
    value: JsonValue,
    *,
    samples: int,
    max_depth: int,
    byte_limit: int,
) -> JsonObject:
    summary: JsonObject = {
        "type": _shape_label(value),
        "serialized_bytes": len(to_json(value)),
        "shape": _describe_shape(value, depth=0, max_depth=max_depth),
        "truncated": _inspection_is_truncated(value, depth=0, max_depth=max_depth),
    }
    cardinality = _cardinality(value)
    if cardinality is not None:
        summary["cardinality"] = cardinality
    if isinstance(value, dict):
        ranked_fields = heapq.nsmallest(
            SHAPE_FIELD_LIMIT,
            ((str(key), len(to_json(item))) for key, item in value.items()),
            key=lambda item: (-item[1], item[0]),
        )
        summary["field_sizes"] = [
            {"path": f"$.{_bounded_key(key, index)}", "bytes": size}
            for index, (key, size) in enumerate(ranked_fields)
        ]
    scalar_types: dict[str, set[str]] = {}
    _collect_scalar_types(
        value,
        path="$",
        depth=0,
        max_depth=max_depth,
        result=scalar_types,
    )
    summary["scalar_types"] = JSON_VALUE_ADAPTER.validate_python({
        path: sorted(types) for path, types in sorted(scalar_types.items())
    })
    if isinstance(value, list):
        sampled_items = list(islice(value, INSPECT_COLLECTION_LIMIT))
        object_items = [item for item in sampled_items if isinstance(item, dict)]
        if object_items:
            key_counts = Counter(
                str(key) for item in object_items for key in islice(item, SHAPE_FIELD_LIMIT)
            )
            summary["common_keys"] = JSON_VALUE_ADAPTER.validate_python(
                [
                    _bounded_key(key, index)
                    for index, (key, count) in enumerate(
                        sorted(key_counts.items(), key=lambda item: (-item[1], item[0]))
                    )
                    if count == len(object_items)
                ][:SHAPE_FIELD_LIMIT]
            )
        summary["item_types"] = JSON_VALUE_ADAPTER.validate_python(
            sorted({_shape_label(item) for item in sampled_items})
        )
    if samples > 0:
        raw_samples = value[:samples] if isinstance(value, list) else [value]
        summary["samples"] = [
            _bounded_sample(item, depth=0, max_depth=max_depth) for item in raw_samples
        ]
    return _fit_inspection_budget(summary, byte_limit)


def _fit_inspection_budget(summary: JsonObject, byte_limit: int) -> JsonObject:
    candidates = [
        summary,
        {**summary, "samples": [], "diagnostic_truncated": True},
        {
            key: value
            for key, value in summary.items()
            if key not in {"samples", "field_sizes", "scalar_types", "common_keys"}
        }
        | {"diagnostic_truncated": True},
        {
            "type": summary["type"],
            "serialized_bytes": summary["serialized_bytes"],
            "shape": summary["type"],
            "truncated": True,
            "diagnostic_truncated": True,
        },
    ]
    for candidate in candidates:
        if len(to_json(candidate)) <= byte_limit:
            return candidate
    raise ValueError(f"inspection byte limit must be at least {len(to_json(candidates[-1]))}")


def _bounded_key(value: str, index: int) -> str:
    if len(value) <= INSPECT_KEY_LIMIT:
        return value
    suffix = f"…[{index}]"
    return f"{value[: INSPECT_KEY_LIMIT - len(suffix)]}{suffix}"


def _collect_scalar_types(
    value: JsonValue,
    *,
    path: str,
    depth: int,
    max_depth: int,
    result: dict[str, set[str]],
) -> None:
    if len(result) >= SHAPE_FIELD_LIMIT:
        return
    if depth >= max_depth and isinstance(value, (dict, list)):
        return
    if isinstance(value, dict):
        for index, (key, item) in enumerate(islice(value.items(), INSPECT_COLLECTION_LIMIT)):
            _collect_scalar_types(
                item,
                path=f"{path}.{_bounded_key(str(key), index)}",
                depth=depth + 1,
                max_depth=max_depth,
                result=result,
            )
        return
    if isinstance(value, list):
        for item in islice(value, INSPECT_COLLECTION_LIMIT):
            _collect_scalar_types(
                item,
                path=f"{path}[]",
                depth=depth + 1,
                max_depth=max_depth,
                result=result,
            )
        return
    result.setdefault(path, set()).add(_shape_label(value))


def _describe_shape(value: JsonValue, *, depth: int, max_depth: int) -> JsonValue:
    if depth >= max_depth:
        return _shape_label(value)
    if isinstance(value, dict):
        fields: JsonObject = {}
        for index, (key, item) in enumerate(islice(value.items(), SHAPE_FIELD_LIMIT)):
            fields[_bounded_key(str(key), index)] = _describe_shape(
                item,
                depth=depth + 1,
                max_depth=max_depth,
            )
        remaining = len(value) - min(len(value), SHAPE_FIELD_LIMIT)
        if remaining > 0:
            fields["<remaining>"] = f"{remaining} more fields"
        return fields
    if isinstance(value, list):
        shapes: list[JsonValue] = []
        seen: set[str] = set()
        for item in islice(value, INSPECT_COLLECTION_LIMIT):
            shape = _describe_shape(item, depth=depth + 1, max_depth=max_depth)
            fingerprint = to_json(shape).decode()
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            shapes.append(shape)
        return {"items": shapes, "count": len(value)}
    return _shape_label(value)


def _bounded_sample(value: JsonValue, *, depth: int, max_depth: int) -> JsonValue:
    if depth >= max_depth:
        return _shape_label(value)
    if isinstance(value, str):
        return value if len(value) <= INSPECT_STRING_LIMIT else f"{value[:INSPECT_STRING_LIMIT]}…"
    if isinstance(value, dict):
        object_sample: JsonObject = {}
        for index, (key, item) in enumerate(islice(value.items(), INSPECT_COLLECTION_LIMIT)):
            object_sample[_bounded_key(str(key), index)] = _bounded_sample(
                item,
                depth=depth + 1,
                max_depth=max_depth,
            )
        if len(value) > INSPECT_COLLECTION_LIMIT:
            object_sample["<remaining>"] = len(value) - INSPECT_COLLECTION_LIMIT
        return object_sample
    if isinstance(value, list):
        list_sample: list[JsonValue] = [
            _bounded_sample(item, depth=depth + 1, max_depth=max_depth)
            for item in islice(value, INSPECT_COLLECTION_LIMIT)
        ]
        if len(value) > INSPECT_COLLECTION_LIMIT:
            list_sample.append(f"<{len(value) - INSPECT_COLLECTION_LIMIT} more items>")
        return list_sample
    return value


def _inspection_is_truncated(value: JsonValue, *, depth: int, max_depth: int) -> bool:
    if depth >= max_depth and isinstance(value, (dict, list)):
        return True
    if isinstance(value, dict):
        return len(value) > SHAPE_FIELD_LIMIT or any(
            len(str(key)) > INSPECT_KEY_LIMIT
            or _inspection_is_truncated(item, depth=depth + 1, max_depth=max_depth)
            for key, item in islice(value.items(), SHAPE_FIELD_LIMIT)
        )
    if isinstance(value, list):
        return len(value) > INSPECT_COLLECTION_LIMIT or any(
            _inspection_is_truncated(item, depth=depth + 1, max_depth=max_depth)
            for item in islice(value, INSPECT_COLLECTION_LIMIT)
        )
    if isinstance(value, str):
        return len(value) > INSPECT_STRING_LIMIT
    return False


def _cardinality(value: JsonValue) -> int | None:
    if isinstance(value, (dict, list, str)):
        return len(value)
    return None


def _shape_label(value: JsonValue) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return f"array[{len(value)}]"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    assert_never(value)


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
        @override
        def visit_Call(self, node: ast.Call) -> ast.AST:
            self.generic_visit(node)
            function = node.func
            if isinstance(function, ast.Name) and function.id == "inspect_json":
                external_call = ast.Call(
                    func=ast.Name(id=INSPECT_JSON_EXTERNAL, ctx=ast.Load()),
                    args=node.args,
                    keywords=node.keywords,
                )
                return ast.copy_location(ast.Await(value=external_call), node)
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
