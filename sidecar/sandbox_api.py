from __future__ import annotations

INSPECT_JSON_NAME = "inspect_json"
EXPECT_OBJECT_NAME = "expect_object"
EXPECT_LIST_NAME = "expect_list"
EXPECT_STRING_NAME = "expect_string"
EXPECT_INTEGER_NAME = "expect_integer"

SANDBOX_FUNCTION_EXTERNALS = {
    INSPECT_JSON_NAME: "__codemcp_inspect_json",
    EXPECT_OBJECT_NAME: "__codemcp_expect_object",
    EXPECT_LIST_NAME: "__codemcp_expect_list",
    EXPECT_STRING_NAME: "__codemcp_expect_string",
    EXPECT_INTEGER_NAME: "__codemcp_expect_integer",
}

STUB_IMPORTS = "from typing import Literal, Never, NotRequired, TypeAlias, TypedDict"
JSON_TYPE_STUBS = (
    "JsonScalar: TypeAlias = bool | int | float | str | None",
    'JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]',
)
SANDBOX_CAPABILITY_STUB = (
    "# Prebound SDK/helpers are not imported. Sandbox: asyncio.gather.\n"
    "# Unavailable: collections.Counter, base64, gzip, asyncio.create_task,\n"
    "# __import__. Use dict counts."
)
INSPECT_JSON_STUB = (
    "def inspect_json(value: JsonValue, *, "
    "samples: Literal[1, 2, 3] = 2, "
    "max_depth: Literal[1, 2, 3, 4, 5, 6] = 3) -> JsonValue: ..."
)
NARROWING_HELPER_STUBS = (
    "def expect_object(value: JsonValue) -> dict[str, JsonValue]: ...",
    "def expect_list(value: JsonValue) -> list[JsonValue]: ...",
    "def expect_string(value: JsonValue) -> str: ...",
    "def expect_integer(value: JsonValue) -> int: ...",
)
STUB_PRELUDE = "\n\n".join((
    STUB_IMPORTS,
    *JSON_TYPE_STUBS,
    SANDBOX_CAPABILITY_STUB,
    INSPECT_JSON_STUB,
    *NARROWING_HELPER_STUBS,
))
