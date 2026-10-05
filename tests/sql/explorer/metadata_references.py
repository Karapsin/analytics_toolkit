from __future__ import annotations

import pandas as pd
import pytest
from analytics_toolkit.sql.backends.metadata import build_gp_search_path_query
from analytics_toolkit.sql_explorer import completion
from analytics_toolkit.sql_explorer.completion import (
    ClickHouseCompletionProvider,
    CompletionCoordinator,
    GreenplumCompletionProvider,
    TrinoCompletionProvider,
)
from analytics_toolkit.sql_explorer.journal_metadata import describe_sql
from analytics_toolkit.sql_explorer.journal_namespace_commands import (
    _rename,
    command_tokens,
    namespace_command,
)
from analytics_toolkit.sql_explorer.metadata_usage import column_in_scopes, event_usage

from tests.sql.explorer.completion import FakeProvider, _wait_for


@pytest.mark.parametrize(
    ("backend", "sql", "expected"),
    [
        ("trino", "SHOW SCHEMAS FROM lake", ("schema", "lake", "")),
        ("trino", "SHOW TABLES FROM lake.sales LIKE 'orders%'", ("table", "lake", "sales")),
        ("ch", "SHOW TABLES FROM sales", ("table", "", "sales")),
        ("trino", "USE lake.sales; SELECT * FROM orders", ("table", "lake", "sales")),
        ("ch", "USE sales; SELECT * FROM orders", ("table", "", "sales")),
        ("gp", 'select * from "Sales"."Orders"', ("table", "", "Sales")),
    ],
)
def test_namespace_statements_and_effective_defaults(backend, sql, expected) -> None:
    event = {
        "backend": backend,
        "context": {"metadata": {"identity": "test"}},
        "statements": describe_sql(sql, backend),
    }
    scopes, _ = event_usage(event, {}, {}, lambda name, context: None)
    assert expected in scopes


def test_search_path_context_is_passed_to_bounded_resolver() -> None:
    calls = []

    def resolve(name, context):
        calls.append((name, context["search_path"]))
        return "", "sales"

    event = {
        "backend": "gp",
        "context": {"metadata": {"identity": "test"}},
        "statements": describe_sql("SET search_path TO sales; SELECT * FROM orders", "gp"),
    }
    scopes, _ = event_usage(event, {}, {}, resolve)
    assert calls == [("orders", ["sales"])]
    assert ("table", "", "sales") in scopes


@pytest.mark.parametrize(
    ("backend", "sql", "expected"),
    [
        ("trino", "CREATE CATALOG lake USING memory", {("schema", "lake", "")}),
        (
            "ch",
            "RENAME TABLE sales.old TO archive.new",
            {("table", "", "sales"), ("table", "", "archive")},
        ),
        (
            "gp",
            "ALTER TABLE sales.orders SET SCHEMA archive",
            {("table", "", "sales"), ("table", "", "archive")},
        ),
    ],
)
def test_backend_namespace_ddl_missing_from_main_parser(backend, sql, expected) -> None:
    event = {"backend": backend, "context": {}, "statements": describe_sql(sql, backend)}
    scopes, _ = event_usage(event, {}, {}, lambda name, context: None)
    assert expected <= scopes.keys()
    assert all(scopes[key] == "ddl" for key in expected)


def test_multiple_search_path_entries_use_structured_sql_parser() -> None:
    statements = describe_sql('SET search_path TO "Sales", public', "gp")
    assert statements[0]["namespace"] == {"search_path": ["Sales", "public"]}


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE sales.copy AS SELECT * FROM archive.orders",
        "SELECT * INTO sales.copy FROM archive.orders",
        "ALTER TABLE sales.orders RENAME TO renamed",
        "DROP TABLE sales.orders",
    ],
)
def test_ddl_targets_are_scoped_and_sources_are_usage_only(sql) -> None:
    snapshots = {
        ("schema", "", ""): ("sales", "archive"),
        ("table", "", "archive"): ("orders",),
        ("table", "", "sales"): ("orders",),
    }
    event = {"backend": "gp", "context": {}, "statements": describe_sql(sql, "gp")}
    scopes, _ = event_usage(event, snapshots, {}, lambda name, context: None)
    assert scopes[("table", "", "sales")] == "ddl"
    assert scopes.get(("table", "", "archive"), "") == ""


def test_namespace_drop_does_not_rescan_deleted_namespace() -> None:
    event = {
        "backend": "trino",
        "context": {},
        "statements": describe_sql("DROP SCHEMA lake.sales", "trino"),
    }
    scopes, _ = event_usage(
        event,
        {("catalog", "", ""): ("lake",), ("schema", "lake", ""): ("sales",)},
        {},
        lambda name, context: None,
    )
    assert scopes[("schema", "lake", "")] == "ddl"
    assert ("table", "lake", "sales") not in scopes


def test_trino_identifier_case_does_not_trigger_false_missing_refresh() -> None:
    snapshots = {
        ("catalog", "", ""): ("lake",),
        ("schema", "lake", ""): ("sales",),
        ("table", "lake", "sales"): ("orders",),
    }
    event = {
        "backend": "trino",
        "context": {},
        "statements": describe_sql("SELECT * FROM LAKE.SALES.ORDERS", "trino"),
    }
    scopes, objects = event_usage(event, snapshots, {}, lambda name, context: None)
    assert objects == [("table", "lake", "sales", "orders")]
    assert all(reason == "" for reason in scopes.values())


def test_unqualified_drop_uses_saved_candidates_in_backend_search_path_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshots = {
        ("schema", "", ""): ("sales", "archive"),
        ("table", "", "sales"): ("orders",),
        ("table", "", "archive"): ("orders",),
    }
    provider = GreenplumCompletionProvider()
    queries = []

    def frame(alias, query):
        queries.append(query)
        return pd.DataFrame([["sales"], ["archive"]])

    monkeypatch.setattr(completion, "_metadata_frame", frame)
    event = {
        "backend": "gp",
        "context": {"metadata": {"identity": "test"}},
        "statements": describe_sql("DROP TABLE orders", "gp"),
    }
    scopes, _ = event_usage(
        event,
        snapshots,
        {},
        lambda name, context: provider.resolve_reference(
            "gp", name, candidates=context.get("candidates")
        ),
    )
    assert scopes[("table", "", "sales")] == "ddl"
    assert ("table", "", "archive") not in scopes
    assert queries == ["SELECT unnest(current_schemas(true))"]


def test_backend_resolution_uses_names_only_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def frame(alias, query):
        calls.append(query)
        return (
            pd.DataFrame([["lake", "sales"]])
            if "current_catalog" in query
            else pd.DataFrame([["sales"]])
        )

    monkeypatch.setattr(completion, "_metadata_frame", frame)
    gp = GreenplumCompletionProvider()
    assert gp.resolve_reference("gp", "orders") == ("", "sales")
    assert gp.resolve_reference("gp", "orders", search_path=["sales", "$user"]) == ("", "sales")
    assert ClickHouseCompletionProvider().resolve_reference("ch", "orders") == ("", "sales")
    assert TrinoCompletionProvider().resolve_reference("trino", "orders") == ("lake", "sales")
    assert "to_regclass" in calls[0]
    assert "array_position" in calls[1]
    assert not any("row_count" in query or "relation_size" in query for query in calls)
    monkeypatch.setattr(completion, "_metadata_frame", lambda *args: pd.DataFrame())
    assert gp.resolve_reference("gp", "missing") is None
    assert ClickHouseCompletionProvider().resolve_reference("ch", "missing") is None
    assert TrinoCompletionProvider().resolve_reference("trino", "missing") is None


def test_ddl_column_invalidation_retains_unrelated_schema_columns() -> None:
    coordinator = CompletionCoordinator("gp", "gp", provider=FakeProvider())
    coordinator._table_columns = {"sales.orders": ("changed",), "archive.orders": ("keep",)}
    try:
        coordinator.invalidate_scopes({("table", "", "sales"): "ddl", ("schema", "", ""): ""})
        assert coordinator._table_columns == {"archive.orders": ("keep",)}
        assert column_in_scopes("orders", "gp", {("table", "", "sales")})
        assert not column_in_scopes("other.archive.orders", "trino", {("table", "lake", "sales")})
    finally:
        coordinator.stop()
        _wait_for(lambda: coordinator.is_stopped)


def test_explicit_search_path_escaping_and_missing_candidate(monkeypatch):
    query = build_gp_search_path_query(["$user", "sales'archive"])
    assert query == "SELECT unnest(ARRAY[current_user,'sales''archive'])"
    monkeypatch.setattr(completion, "_metadata_frame", lambda *args: pd.DataFrame([["public"]]))
    assert (
        GreenplumCompletionProvider().resolve_reference(
            "gp", "orders", search_path=["public"], candidates=[("", "sales")]
        )
        is None
    )
    assert "o''%" in completion._table_condition("o'")


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        ("SET SESSION search_path TO sales", ["sales"]),
        ("SET LOCAL search_path TO sales, public", ["sales", "public"]),
        ("SET statement_timeout TO 1000", None),
    ],
)
def test_search_path_session_forms(statement, expected):
    description = describe_sql(statement, "gp")[0]
    assert description.get("namespace", {}).get("search_path") == expected


def test_show_unknown_forms_and_qualified_rename_are_best_effort():
    assert describe_sql("SHOW TABLES FROM Sales", "gp")[0]["objects"][0]["name"] == "sales"
    assert describe_sql("SHOW COLUMNS FROM sales.orders", "trino")[0]["objects"] == []
    assert describe_sql("SHOW TABLES LIKE 'x'", "trino")[0]["objects"] == []
    references = describe_sql("ALTER TABLE sales.orders RENAME TO archive.renamed", "gp")[0]
    assert references["objects"][-1]["schema"] == "archive"


def test_catalog_if_clauses_and_multiple_rename_pairs():
    assert namespace_command("SET LOCAL search_path", "gp") is None
    for sql in ("CREATE CATALOG IF NOT EXISTS lake USING memory", "DROP CATALOG IF EXISTS lake"):
        assert namespace_command(sql, "trino")["objects"][0]["name"] == "lake"
    result = namespace_command("RENAME TABLE sales.old TO new, archive.old TO renamed", "ch")
    assert [(obj["schema"], obj["name"]) for obj in result["objects"]] == [
        ("sales", "old"),
        ("sales", "new"),
        ("archive", "old"),
        ("archive", "renamed"),
    ]
    assert namespace_command("RENAME TABLE sales.old INTO new", "ch") is None
    assert namespace_command("RENAME TABLE sales.old", "ch") is None
    # Empty/incomplete commands can occur in legacy best-effort journal metadata.
    assert _rename("RENAME TABLE", command_tokens("RENAME TABLE"), "clickhouse")["objects"] == []


def test_legacy_partial_objects_and_namespace_drop_are_conservative():
    event = {
        "backend": "trino",
        "context": {},
        "statements": [
            {
                "action": "drop",
                "objects": [
                    {"kind": "table", "name": None},
                    {"kind": "function", "name": "fn"},
                    {"kind": "catalog", "name": "lake"},
                ],
            }
        ],
    }
    scopes, objects = event_usage(event, {("catalog", "", ""): ("lake",)}, {}, lambda *args: None)
    assert scopes == {("catalog", "", ""): "ddl"}
    assert objects == [("catalog", "lake", "", "lake")]
    assert column_in_scopes('"unterminated', "gp", {("table", "", "sales")})


@pytest.mark.parametrize("backend", ["gp", "ch"])
def test_unqualified_create_without_saved_target_uses_current_namespace(backend):
    event = {
        "backend": backend,
        "context": {"metadata": {"identity": "test", "database": "sales"}},
        "statements": describe_sql("CREATE TABLE new_table (id INT)", backend),
    }
    scopes, _ = event_usage(event, {}, {}, lambda *args: ("", "sales"))
    assert scopes[("table", "", "sales")] == "ddl"
