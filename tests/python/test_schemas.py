from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastmcp.client.auth import OAuth
from mcp import types as mcp_types
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import ValidationError

from sidecar import schemas
from sidecar.schemas import ToolCatalog, normalize_mcp_config


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


def test_normalize_config_supports_transports_and_skips_disabled(tmp_path: Path) -> None:
    normalized = normalize_mcp_config(
        {
            "mcpServers": {
                "stdio": {
                    "command": "example-server",
                    "args": ["--stdio"],
                    "directTools": True,
                    "lifecycle": "lazy",
                    "idleTimeout": 10,
                },
                "http": {
                    "type": "http",
                    "url": "https://example.test/mcp",
                    "headers": {"x-test": "yes"},
                },
                "events": {
                    "type": "sse",
                    "url": "https://example.test/sse",
                },
                "disabled": {"command": "never", "disabled": True},
            }
        },
        oauth_storage_dir=tmp_path / "oauth",
    )

    assert set(normalized.config.mcpServers) == {"stdio", "http", "events"}
    assert [server.transport for server in normalized.servers] == ["stdio", "http", "sse"]
    stdio = normalized.config.mcpServers["stdio"]
    assert stdio.command == "example-server"
    assert "directTools" not in stdio.model_extra


@pytest.mark.asyncio
async def test_oauth_uses_native_fastmcp_persistence_and_refresh(tmp_path: Path) -> None:
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


def test_config_requires_enabled_servers(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="No enabled"):
        normalize_mcp_config(
            {"only": {"command": "unused", "disabled": True}},
            oauth_storage_dir=tmp_path,
        )


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
    matches = catalog.search("work id")
    assert [match.name for match in matches] == ["linear_get_issue"]
    assert matches[0].call == "linear.get_issue"
    assert matches[0].signature.startswith("await linear.get_issue(")
    assert "call_tool" not in catalog.type_stubs
    assert "inputSchema" not in matches[0].model_dump_json()

    compact = catalog.get_schema(["linear_get_issue"])[0]
    assert "LinearGetIssueArgs" in compact.stub
    assert "input_schema" not in compact.model_dump_json()
    assert "output_schema" not in compact.model_dump_json()


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
                    'List issues in the user\'s Linear workspace. For my issues, use '
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


def test_catalog_search_ranks_failed_multi_word_queries(
    representative_search_catalog: ToolCatalog,
) -> None:
    expected_rankings = {
        "Grafana dashboards metrics query": [
            "grafana_search_dashboards",
            "grafana_get_dashboard_panel_queries",
            "grafana_get_dashboard_summary",
            "grafana_query_prometheus",
            "grafana_list_datasources",
        ],
        "dashboard summary panel datasource metrics query time range prometheus": [
            "grafana_get_dashboard_panel_queries",
            "grafana_get_dashboard_summary",
            "grafana_search_dashboards",
            "grafana_query_prometheus",
        ],
        "alerts history firing active": [
            "grafana_alerting_manage_rules",
            "grafana_alerting_manage_routing",
        ],
    }

    for query, expected in expected_rankings.items():
        matches = representative_search_catalog.search(query, limit=5)
        assert [match.name for match in matches] == expected, query
        assert [match.call for match in matches] == [
            name.replace("_", ".", 1) for name in expected
        ], query
        assert all(match.server == "grafana" for match in matches), query


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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_extract = schemas.process.extract
    captured_candidates: list[str] = []

    def capture_extract(query: str, choices: dict[str, str], **kwargs: object) -> list:
        captured_candidates.extend(choices)
        return original_extract(query, choices, **kwargs)

    monkeypatch.setattr(schemas.process, "extract", capture_extract)

    matches = representative_search_catalog.search("dashboards", server="grafana")

    assert captured_candidates == sorted(
        spec.name
        for spec in representative_search_catalog.tools.values()
        if spec.server == "grafana"
    )
    assert matches
    assert all(match.server == "grafana" for match in matches)


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
    with pytest.raises(ValueError, match="Unknown tools"):
        catalog.get_schema(["linear_missing"])


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
    assert ToolCatalog.from_server_tools(
        {
            "my-server": [
                make_tool("class", empty_schema),
                make_tool("get-issue", empty_schema),
                make_tool("get_issue", empty_schema),
            ],
            "my_server": [make_tool("list", empty_schema)],
        }
    ).facade_calls == catalog.facade_calls
