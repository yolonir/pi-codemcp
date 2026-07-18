from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastmcp.client.auth import OAuth
from mcp import types as mcp_types
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import ValidationError

from sidecar.chains import ChainStore
from sidecar.mcp_config import normalize_mcp_config
from sidecar.models import NormalizedServerInfo
from sidecar.tool_catalog import ToolCatalog


def test_normalized_server_info_rejects_unknown_transport_and_fields() -> None:
    with pytest.raises(ValidationError, match="transport"):
        NormalizedServerInfo.model_validate(
            {
                "name": "example",
                "transport": "websocket",
                "config_fingerprint": "fingerprint",
            }
        )
    with pytest.raises(ValidationError, match="unexpected"):
        NormalizedServerInfo.model_validate(
            {
                "name": "example",
                "transport": "http",
                "config_fingerprint": "fingerprint",
                "unexpected": True,
            }
        )


def make_tool(
    name: str,
    input_schema: dict,
    output_schema: dict | None = None,
    description: str | None = None,
) -> mcp_types.Tool:
    return mcp_types.Tool(
        name=name,
        description=description,
        inputSchema=input_schema,
        outputSchema=output_schema,
    )


def test_normalize_config_supports_transports_and_skips_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEMCP_TEST_TOKEN", "secret-token")
    monkeypatch.setenv("UNLISTED_CODEMCP_SECRET", "must-not-leak")
    monkeypatch.setenv("MY_PI_MCP_ENV_ALLOWLIST", "CODEMCP_TEST_TOKEN")
    normalized = normalize_mcp_config(
        {
            "mcpServers": {
                "stdio": {
                    "command": "example-server",
                    "args": ["--stdio"],
                    "directTools": True,
                    "lifecycle": "lazy",
                    "idleTimeout": 10,
                    "env": {"EXPLICIT_VALUE": "configured"},
                },
                "http": {
                    "type": "http",
                    "url": "https://example.test/mcp",
                    "headers": {
                        "x-test": "yes",
                        "authorization": "Bearer ${CODEMCP_TEST_TOKEN}",
                    },
                },
                "events": {
                    "type": "sse",
                    "url": "https://example.test/sse",
                },
                "disabled": {"command": "never", "disabled": True},
                "not-enabled": {"command": "never", "enabled": False},
            }
        },
        oauth_storage_dir=tmp_path / "oauth",
    )

    assert set(normalized.config.mcpServers) == {"stdio", "http", "events"}
    assert [server.transport for server in normalized.servers] == [
        "stdio",
        "http",
        "sse",
        "stdio",
        "stdio",
    ]
    assert [server.enabled for server in normalized.servers] == [
        True,
        True,
        True,
        False,
        False,
    ]
    stdio = normalized.config.mcpServers["stdio"]
    assert stdio.command == "example-server"
    assert stdio.env["EXPLICIT_VALUE"] == "configured"
    assert stdio.env["CODEMCP_TEST_TOKEN"] == "secret-token"
    assert "UNLISTED_CODEMCP_SECRET" not in stdio.env
    assert "directTools" not in stdio.model_extra
    http_server = normalized.config.mcpServers["http"]
    assert http_server.headers["authorization"] == "Bearer secret-token"


@pytest.mark.asyncio
async def test_oauth_uses_native_fastmcp_persistence_and_refresh(
    tmp_path: Path,
) -> None:
    raw = {
        "linear": {
            "type": "http",
            "url": "https://mcp.linear.app/mcp",
            "auth": "oauth",
        }
    }
    first = normalize_mcp_config(raw, oauth_storage_dir=tmp_path)
    first_auth = first.config.mcpServers["linear"].auth
    assert isinstance(first_auth, OAuth)

    await first_auth.token_storage_adapter.set_client_info(
        OAuthClientInformationFull(
            client_id="test-client",
            **first_auth.context.client_metadata.model_dump(exclude_none=True),
        )
    )
    await first_auth.token_storage_adapter.set_tokens(
        OAuthToken(
            access_token="expired-access",
            token_type="Bearer",
            refresh_token="refresh-token",
            expires_in=-1,
        )
    )

    flow = first_auth.async_auth_flow(
        httpx.Request("POST", "https://mcp.linear.app/mcp")
    )
    refresh_request = await anext(flow)
    assert str(refresh_request.url) == "https://mcp.linear.app/token"
    assert b"grant_type=refresh_token" in refresh_request.content
    assert b"refresh_token=refresh-token" in refresh_request.content

    protected_request = await flow.asend(
        httpx.Response(
            200,
            json={
                "access_token": "refreshed-access",
                "token_type": "Bearer",
                "refresh_token": "next-refresh-token",
                "expires_in": 3600,
            },
            request=refresh_request,
        )
    )
    assert protected_request.headers["Authorization"] == "Bearer refreshed-access"
    with pytest.raises(StopAsyncIteration):
        await flow.asend(httpx.Response(200, request=protected_request))

    second = normalize_mcp_config(raw, oauth_storage_dir=tmp_path)
    second_auth = second.config.mcpServers["linear"].auth
    assert isinstance(second_auth, OAuth)
    restored = await second_auth.token_storage_adapter.get_tokens()
    assert restored is not None
    assert restored.access_token == "refreshed-access"
    assert restored.refresh_token == "next-refresh-token"


def test_config_allows_every_server_to_be_disabled(tmp_path: Path) -> None:
    normalized = normalize_mcp_config(
        {"only": {"command": "unused", "disabled": True}},
        oauth_storage_dir=tmp_path,
    )

    assert normalized.config.mcpServers == {}
    assert [(server.name, server.enabled) for server in normalized.servers] == [
        ("only", False)
    ]


def test_catalog_namespaces_single_server_and_searches_compactly() -> None:
    catalog = ToolCatalog.from_mcp_tools(
        [
            make_tool(
                "get_issue",
                {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                    "required": ["id"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {"title": {"type": "string"}},
                    "required": ["title"],
                },
                "Retrieve a work item",
            )
        ],
        ["linear"],
    )

    assert list(catalog.tools) == ["linear_get_issue"]
    assert catalog.tools["linear_get_issue"].backend_name == "get_issue"
    matches = catalog.search("work id", detail="full")
    assert [match.name for match in matches] == ["linear_get_issue"]
    assert matches[0].call == "linear.get_issue"
    assert matches[0].signature.startswith("await linear.get_issue(")
    assert "call_tool" not in catalog.type_stubs
    assert "inputSchema" not in matches[0].model_dump_json()

    assert "LinearGetIssueArgs" in matches[0].stub
    assert "input_schema" not in matches[0].model_dump_json()
    assert "output_schema" not in matches[0].model_dump_json()


def test_untyped_schema_uses_recursive_json_value_instead_of_any() -> None:
    catalog = ToolCatalog.from_server_tools(
        {
            "clickhouse": [
                make_tool(
                    "list_databases",
                    {"type": "object", "additionalProperties": True},
                )
            ]
        }
    )

    spec = catalog.tools["clickhouse_list_databases"]
    assert spec.signature == (
        "await clickhouse.list_databases(arguments: ClickhouseListDatabasesArgs) "
        "-> ClickhouseListDatabasesResult"
    )
    assert "ClickhouseListDatabasesArgs: TypeAlias = dict[str, JsonValue]" in spec.stub
    assert "ClickhouseListDatabasesResult: TypeAlias = JsonValue" in spec.stub
    assert "Any" not in catalog.type_stubs


def test_recursive_json_schema_refs_stop_at_json_value() -> None:
    recursive_value = {
        "anyOf": [
            {"type": "string"},
            {"type": "number"},
            {"type": "boolean"},
            {"type": "null"},
            {
                "type": "object",
                "additionalProperties": {"$ref": "#/definitions/value"},
            },
            {
                "type": "array",
                "items": {"$ref": "#/definitions/value"},
            },
        ]
    }
    catalog = ToolCatalog.from_server_tools(
        {
            "docs": [
                make_tool(
                    "search_docs",
                    {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                    output_schema={
                        "type": "object",
                        "properties": {
                            "results": {
                                "type": "array",
                                "items": {"$ref": "#/definitions/value"},
                            }
                        },
                        "required": ["results"],
                        "definitions": {"value": recursive_value},
                    },
                )
            ]
        }
    )

    stub = catalog.tools["docs_search_docs"].stub
    assert (
        "results: list[str | float | bool | None | dict[str, JsonValue] | list[JsonValue]]"
        in stub
    )
    assert "Any" not in catalog.type_stubs


def make_search_fixture_tool(
    name: str,
    description: str,
    property_names: tuple[str, ...],
) -> mcp_types.Tool:
    # Search indexes property names, not their validation keywords. Keeping only those
    # names makes this failed-session metadata snapshot compact without changing its
    # searchable corpus.
    return make_tool(
        name,
        {
            "type": "object",
            "properties": {prop: {"type": "string"} for prop in property_names},
        },
        description=description,
    )


@pytest.fixture
def representative_search_catalog() -> ToolCatalog:
    # Names, descriptions, and input properties are verbatim searchable metadata from
    # the Grafana and Linear capabilities available in the reviewed Pi session.
    return ToolCatalog.from_server_tools(
        {
            "grafana": [
                make_search_fixture_tool(
                    "search_dashboards",
                    "Search for Grafana dashboards by a query string. Returns a list "
                    "of matching dashboards with details like title, UID, folder, "
                    "tags, and URL.",
                    ("limit", "page", "query"),
                ),
                make_search_fixture_tool(
                    "get_dashboard_panel_queries",
                    "Retrieve panel queries from a Grafana dashboard. Supports all "
                    "datasource types (Prometheus, Loki, CloudWatch, SQL, etc.) and "
                    "row-nested panels. Optionally filter to a specific panel by ID "
                    "with `panelId`. Optionally provide `variables` for template "
                    "variable substitution, which populates `processedQuery` and "
                    "`requiredVariables` fields. Returns an array of objects with "
                    "fields: title, query (raw expression), datasource (object with "
                    "uid and type), and optionally processedQuery, refId, and "
                    "requiredVariables.",
                    ("panelId", "uid", "variables"),
                ),
                make_search_fixture_tool(
                    "get_dashboard_summary",
                    "Get a compact summary of a dashboard including title\\, panel "
                    "count\\, panel types\\, variables\\, and other metadata without "
                    "the full JSON. Use this for dashboard overview and planning "
                    "modifications without consuming large context windows.",
                    ("uid",),
                ),
                make_search_fixture_tool(
                    "query_prometheus",
                    "WORKFLOW: list_prometheus_metric_names -> "
                    "list_prometheus_label_values -> query_prometheus. Query a "
                    "PromQL-compatible datasource (Prometheus, Thanos, Mimir, Cloud "
                    "Monitoring, etc.) using a PromQL expression. Supports instant "
                    "queries (single point) and range queries (time range). Time: "
                    "RFC3339 or relative expressions like 'now'\\, 'now-1h'.",
                    (
                        "datasourceUid",
                        "endTime",
                        "expr",
                        "projectName",
                        "queryType",
                        "startTime",
                        "stepSeconds",
                    ),
                ),
                make_search_fixture_tool(
                    "alerting_manage_rules",
                    "List and inspect Grafana alert rules with filtering capabilities.\n\n"
                    "When to use:\n"
                    "- Understanding why an alert is or isn't firing\n"
                    "- Auditing alert rule configuration (queries, conditions, labels, "
                    "notification settings)\n"
                    "- Finding alert rules by state, folder, group, or name\n"
                    "- Comparing rule versions to see what changed\n\n"
                    "When NOT to use:\n"
                    "- Checking how alerts are routed to receivers (use "
                    "alerting_manage_routing)\n"
                    "- Modifying or creating alert rules (read-only tool)",
                    (
                        "datasource_uid",
                        "folder_uid",
                        "label_selectors",
                        "limit_alerts",
                        "matchers",
                        "operation",
                        "rule_group",
                        "rule_limit",
                        "rule_type",
                        "rule_uid",
                        "search_folder",
                        "search_rule_name",
                        "states",
                    ),
                ),
                make_search_fixture_tool(
                    "list_datasources",
                    "List all configured datasources in Grafana. Use this to "
                    "discover available datasources and their UIDs. Supports "
                    "filtering by type and pagination.",
                    ("limit", "offset", "type"),
                ),
                make_search_fixture_tool(
                    "alerting_manage_routing",
                    "Manage Grafana alerting routing configuration, including "
                    "notification policies, contact points and time intervals.\n\n"
                    "Notification policies define how alerts are grouped, routed, "
                    "and which contact points receive them.\n"
                    "Time intervals define active/mute periods for alert "
                    "notifications.\n\n"
                    "When to use:\n"
                    "- Understanding how alerts are routed to contact "
                    "points/receivers\n"
                    "- Debugging why an alert went to a specific receiver\n"
                    "- Checking grouping, timing, or mute interval settings\n\n"
                    "When NOT to use:\n"
                    "- Checking alert rule configuration or state (use "
                    "alerting_manage_rules)",
                    (
                        "contact_point_title",
                        "datasource_uid",
                        "limit",
                        "name",
                        "operation",
                        "time_interval_name",
                    ),
                ),
            ],
            "linear": [
                make_search_fixture_tool(
                    "list_issues",
                    "List issues in the user's Linear workspace. For my issues, use "
                    '"me" as the assignee. Use "null" for no assignee.',
                    (
                        "limit",
                        "cursor",
                        "orderBy",
                        "query",
                        "team",
                        "state",
                        "cycle",
                        "label",
                        "assignee",
                        "delegate",
                        "project",
                        "release",
                        "priority",
                        "parentId",
                        "createdAt",
                        "updatedAt",
                        "includeArchived",
                    ),
                ),
                make_search_fixture_tool(
                    "list_projects",
                    "List projects in the user's Linear workspace",
                    (
                        "limit",
                        "cursor",
                        "orderBy",
                        "query",
                        "state",
                        "initiative",
                        "team",
                        "member",
                        "label",
                        "createdAt",
                        "updatedAt",
                        "includeMilestones",
                        "includeMembers",
                        "includeArchived",
                    ),
                ),
            ],
        }
    )


def test_catalog_search_replay_queries_have_complete_top_five_recall(
    representative_search_catalog: ToolCatalog,
) -> None:
    expected_matches = {
        "Grafana dashboards metrics query": {
            "grafana_search_dashboards",
            "grafana_get_dashboard_panel_queries",
            "grafana_get_dashboard_summary",
            "grafana_query_prometheus",
            "grafana_list_datasources",
        },
        "dashboard summary panel datasource metrics query time range prometheus": {
            "grafana_get_dashboard_panel_queries",
            "grafana_get_dashboard_summary",
            "grafana_search_dashboards",
            "grafana_query_prometheus",
        },
        "alerts history firing active": {
            "grafana_alerting_manage_rules",
            "grafana_alerting_manage_routing",
        },
    }

    for query, expected in expected_matches.items():
        matches = representative_search_catalog.search(query, limit=5)
        names = {match.name for match in matches}
        assert expected <= names, query
        assert all(match.server == "grafana" for match in matches), query
        assert all(match.score is not None for match in matches)
        assert all(match.matched_fields for match in matches)


def test_catalog_search_normalizes_queries_and_retrieves_exact_identifiers(
    representative_search_catalog: ToolCatalog,
) -> None:
    for normalized_query in (
        "dashboard_panel_queries",
        "dashboard-panel-query",
        "dashboard panel queries!!!",
    ):
        assert representative_search_catalog.search(normalized_query)[0].name == (
            "grafana_get_dashboard_panel_queries"
        )

    assert representative_search_catalog.search("prometheus metric")[0].name == (
        "grafana_query_prometheus"
    )
    for exact_query in (
        "grafana_get_dashboard_summary",
        "grafana.get_dashboard_summary",
    ):
        match = representative_search_catalog.search(exact_query)[0]
        assert match.name == "grafana_get_dashboard_summary"
        assert match.call == "grafana.get_dashboard_summary"


def test_catalog_search_prefilters_server_candidates_exactly(
    representative_search_catalog: ToolCatalog,
) -> None:
    matches = representative_search_catalog.search("dashboards", server="grafana")

    assert matches
    assert all(match.server == "grafana" for match in matches)
    assert all(not match.name.startswith("linear_") for match in matches)


def test_catalog_progressively_discloses_and_paginates_inventory(
    representative_search_catalog: ToolCatalog,
) -> None:
    names = representative_search_catalog.inventory(detail="names", limit=2)
    assert len(names) == 2
    assert all(match.signature is None and match.stub is None for match in names)

    signatures = representative_search_catalog.search(
        "dashboard query", detail="signatures", limit=2
    )
    assert all(match.signature and match.stub is None for match in signatures)

    calls = [match.call for match in signatures]
    inspected = representative_search_catalog.inspect(calls)
    assert [match.call for match in inspected] == calls
    assert all(match.stub and "Args" in match.stub for match in inspected)
    assert "JsonValue: TypeAlias" in representative_search_catalog.stub_prelude
    assert "JsonValue: TypeAlias" not in inspected[0].stub

    first_page = representative_search_catalog.inventory(limit=3, offset=0)
    second_page = representative_search_catalog.inventory(limit=3, offset=3)
    assert {match.call for match in first_page}.isdisjoint(
        match.call for match in second_page
    )


def test_catalog_selective_type_stubs_include_only_referenced_facades(
    representative_search_catalog: ToolCatalog,
) -> None:
    selected = representative_search_catalog.type_stubs_for(
        {"grafana_query_prometheus"}
    )

    assert "class _GrafanaSdk" in selected
    assert "async def query_prometheus" in selected
    assert "GrafanaQueryPrometheusArgs" in selected
    assert "async def search_dashboards" not in selected
    assert "LinearListIssuesArgs" not in selected
    assert len(selected) < len(representative_search_catalog.type_stubs) * 0.35


def test_catalog_inspect_rejects_unknown_calls_with_suggestions(
    representative_search_catalog: ToolCatalog,
) -> None:
    with pytest.raises(ValueError, match="suggestions"):
        representative_search_catalog.inspect(["grafana.get_dashbord_summary"])


def test_catalog_search_indexes_input_property_names_in_isolation() -> None:
    catalog = ToolCatalog.from_server_tools(
        {
            "example": [
                make_search_fixture_tool(
                    "inspect_record",
                    "Retrieve one record.",
                    ("datasourceUid",),
                ),
                make_search_fixture_tool(
                    "inspect_archive",
                    "Retrieve archived records.",
                    ("limit",),
                ),
            ]
        }
    )

    matches = catalog.search("datasource uid")

    assert [match.name for match in matches] == ["example_inspect_record"]


def test_runtime_argument_validation_preserves_omitted_optional_fields() -> None:
    catalog = ToolCatalog.from_server_tools(
        {
            "linear": [
                make_tool(
                    "list_issues",
                    {
                        "type": "object",
                        "properties": {
                            "limit": {"type": "integer"},
                            "query": {"type": "string"},
                            "cursor": {"type": "string"},
                        },
                    },
                )
            ]
        }
    )

    assert catalog.validate_arguments("linear_list_issues", {"limit": 1}) == {
        "limit": 1
    }


def test_catalog_validates_arguments_and_unknown_schema_names() -> None:
    catalog = ToolCatalog.from_mcp_tools(
        [
            make_tool(
                "linear_update_issue",
                {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "priority": {"type": "integer", "enum": [1, 2, 3, 4]},
                    },
                    "required": ["id", "priority"],
                    "additionalProperties": False,
                },
            )
        ],
        ["linear", "grafana"],
    )

    assert catalog.validate_arguments(
        "linear_update_issue", {"id": "x", "priority": 2}
    ) == {"id": "x", "priority": 2}
    with pytest.raises(ValidationError):
        catalog.validate_arguments("linear_update_issue", {"id": "x", "priority": 9})
    assert catalog.search("zzzzzzzz") == []


def test_sdk_facade_aliases_are_valid_stable_and_collision_free() -> None:
    empty_schema = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    catalog = ToolCatalog.from_server_tools(
        {
            "my-server": [
                make_tool("class", empty_schema),
                make_tool("get-issue", empty_schema),
                make_tool("get_issue", empty_schema),
            ],
            "my_server": [make_tool("list", empty_schema)],
        }
    )

    calls = [spec.call for spec in catalog.tools.values()]
    assert len(calls) == len(set(calls))
    assert all(
        namespace.isidentifier() and method.isidentifier()
        for namespace, method in catalog.facade_calls
    )
    assert any(call.endswith(".class_") for call in calls)
    assert len({spec.namespace for spec in catalog.tools.values()}) == 2
    assert (
        ToolCatalog.from_server_tools(
            {
                "my-server": [
                    make_tool("class", empty_schema),
                    make_tool("get-issue", empty_schema),
                    make_tool("get_issue", empty_schema),
                ],
                "my_server": [make_tool("list", empty_schema)],
            }
        ).facade_calls
        == catalog.facade_calls
    )


def test_saved_chain_catalog_preserves_exact_output_contract(tmp_path: Path) -> None:
    chain = ChainStore(tmp_path / "chains").build(
        name="render_title",
        description="Render one title.",
        code='return input["title"]',
        input_schema={
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
            "additionalProperties": False,
        },
        output_schema={"type": "string"},
        dependencies=[],
    )
    catalog = ToolCatalog.from_server_tools({}, saved_chains=[chain])
    spec = catalog.tools["chain_render_title"]

    assert spec.kind == "saved_chain"
    assert spec.call == "chains.render_title"
    assert spec.output_type_name == "ChainRenderTitleResult"
    assert "ChainRenderTitleResult: TypeAlias = str" in catalog.type_stubs
    assert catalog.search("render title")[0].source == "saved_chain"
    assert catalog.validate_saved_chain_result(spec.name, "hello") == "hello"
    with pytest.raises(ValidationError):
        catalog.validate_saved_chain_result(spec.name, {"title": "wrong"})
