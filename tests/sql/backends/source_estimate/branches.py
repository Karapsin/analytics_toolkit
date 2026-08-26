from __future__ import annotations

import importlib
import math
from types import SimpleNamespace
from typing import Any

import pytest
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError

estimate = importlib.import_module("analytics_toolkit.sql.backends.source_estimate")


class FakeCursor:
    def __init__(self, rows: list[Any], *, execute_error: Exception | None = None) -> None:
        self.rows = rows
        self.execute_error = execute_error
        self.executed: list[str] = []
        self.closed = False

    def execute(self, sql: str) -> None:
        self.executed.append(sql)
        if self.execute_error is not None:
            raise self.execute_error

    def fetchall(self) -> list[Any]:
        return self.rows

    def close(self) -> None:
        self.closed = True


class FakeDbapiConnection:
    def __init__(self, cursors: list[FakeCursor]) -> None:
        self.cursors = cursors

    def cursor(self) -> FakeCursor:
        return self.cursors.pop(0)


class FakeAdapter:
    def __init__(self, estimate_result: int | None = None) -> None:
        self.estimate_result = estimate_result
        self.estimate_calls: list[tuple[Any, str, str | None]] = []
        self.rollback_calls: list[Any] = []

    def estimate_source_rows(
        self,
        connection: Any,
        source_sql: str,
        *,
        query_label: str | None,
    ) -> int | None:
        self.estimate_calls.append((connection, source_sql, query_label))
        return self.estimate_result

    def rollback_quietly(self, connection: Any) -> None:
        self.rollback_calls.append(connection)


def _options(**overrides: Any) -> SimpleNamespace:
    values = {
        "progress": True,
        "estimate_total_rows": True,
        "source_sql": "SELECT * FROM source_table;",
        "from_db_backend": "gp",
        "from_db_key": "source_alias",
        "query_label": "coverage",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.parametrize(
    ("progress", "estimate_total_rows"),
    [(False, True), (True, False), (False, False)],
)
def test_estimate_source_rows_skips_disabled_progress(
    monkeypatch: pytest.MonkeyPatch,
    progress: bool,
    estimate_total_rows: bool,
) -> None:
    monkeypatch.setattr(
        estimate,
        "_estimate_source_rows",
        lambda *_args, **_kwargs: pytest.fail("planner must not run"),
    )

    assert (
        estimate.estimate_source_rows(
            _options(progress=progress, estimate_total_rows=estimate_total_rows),
            object(),
        )
        is None
    )


@pytest.mark.parametrize(
    ("planner_result", "message"), [(42, "Using approximate"), (None, "unavailable")]
)
def test_estimate_source_rows_reports_planner_result(
    monkeypatch: pytest.MonkeyPatch,
    planner_result: int | None,
    message: str,
) -> None:
    logs: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(estimate, "_estimate_source_rows", lambda *_args, **_kwargs: planner_result)
    monkeypatch.setattr(
        estimate,
        "time_print",
        lambda text, **kwargs: logs.append((text, kwargs)),
    )

    result = estimate.estimate_source_rows(_options(), object())

    assert result == planner_result
    assert message in logs[0][0]
    assert logs[0][1] == {"connection": "source_alias", "backend": "gp"}


def test_estimate_source_rows_rolls_back_and_logs_parser_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = FakeAdapter()
    connection = object()
    logs: list[str] = []
    monkeypatch.setattr(estimate, "get_backend_adapter", lambda _backend: adapter)
    monkeypatch.setattr(estimate, "time_print", lambda text, **_kwargs: logs.append(text))

    result = estimate.estimate_source_rows(_options(source_sql="SELECT 1; SELECT 2"), connection)

    assert result is None
    assert adapter.rollback_calls == [connection]
    assert "Could not estimate" in logs[0]
    assert "InvalidSqlInputError" in logs[0]


def test_estimate_source_rows_delegates_to_backend_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = FakeAdapter(estimate_result=17)
    connection = object()
    monkeypatch.setattr(
        estimate, "get_backend_adapter", lambda backend: adapter if backend == "trino" else None
    )

    result = estimate._estimate_source_rows(
        "trino",
        connection,
        "SELECT 1",
        query_label="label",
    )

    assert result == 17
    assert adapter.estimate_calls == [(connection, "SELECT 1", "label")]


def test_single_source_statement_normalizes_one_statement() -> None:
    assert estimate._single_source_statement("  SELECT ';' AS value;  ") == "SELECT ';' AS value"


@pytest.mark.parametrize("source_sql", ["", "SELECT 1; SELECT 2;"])
def test_single_source_statement_rejects_zero_or_multiple_statements(source_sql: str) -> None:
    with pytest.raises(InvalidSqlInputError, match="exactly one"):
        estimate._single_source_statement(source_sql)


def test_gp_estimate_prefers_json_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        estimate,
        "_fetch_dbapi_first_column",
        lambda _connection, sql: calls.append(sql) or ['[{"Plan": {"Plan Rows": 12.4}}]'],
    )

    assert estimate._estimate_gp_source_rows(object(), "SELECT 1", query_label=None) == 12
    assert len(calls) == 1
    assert calls[0].startswith("EXPLAIN (FORMAT JSON)")


@pytest.mark.parametrize("json_result", [["not-json"], RuntimeError("json explain failed")])
def test_gp_estimate_falls_back_to_text_and_rolls_back_json_errors(
    monkeypatch: pytest.MonkeyPatch,
    json_result: list[str] | Exception,
) -> None:
    adapter = FakeAdapter()
    connection = object()
    calls = 0

    def fake_fetch(_connection: Any, _sql: str) -> list[str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            if isinstance(json_result, Exception):
                raise json_result
            return json_result
        return ["Seq Scan rows=23.7 width=4"]

    monkeypatch.setattr(estimate, "_fetch_dbapi_first_column", fake_fetch)
    monkeypatch.setattr(estimate, "get_backend_adapter", lambda _backend: adapter)

    assert estimate._estimate_gp_source_rows(connection, "SELECT 1", query_label="x") == 24
    assert adapter.rollback_calls == ([connection] if isinstance(json_result, Exception) else [])


def test_gp_estimate_rolls_back_and_reraises_text_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = FakeAdapter()
    connection = object()
    calls = 0

    def fake_fetch(_connection: Any, _sql: str) -> list[str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return []
        message = "text explain failed"
        raise RuntimeError(message)

    monkeypatch.setattr(estimate, "_fetch_dbapi_first_column", fake_fetch)
    monkeypatch.setattr(estimate, "get_backend_adapter", lambda _backend: adapter)

    with pytest.raises(RuntimeError, match="text explain failed"):
        estimate._estimate_gp_source_rows(connection, "SELECT 1", query_label=None)
    assert adapter.rollback_calls == [connection]


def test_trino_estimate_reads_distributed_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    sqls: list[str] = []
    monkeypatch.setattr(
        estimate,
        "_fetch_dbapi_first_column",
        lambda _connection, sql: sqls.append(sql) or ['{"root": {"outputRowCount": 9}}'],
    )

    assert estimate._estimate_trino_source_rows(object(), "SELECT 1", query_label=None) == 9
    assert sqls[0].startswith("EXPLAIN (TYPE DISTRIBUTED, FORMAT JSON)")


def test_clickhouse_estimate_only_queries_simple_selects(monkeypatch: pytest.MonkeyPatch) -> None:
    queries: list[str] = []
    connection = SimpleNamespace(
        query=lambda sql: (
            queries.append(sql) or SimpleNamespace(result_rows=[(1,)], column_names=["rows"])
        )
    )

    assert (
        estimate._estimate_clickhouse_source_rows(
            connection,
            "SELECT * FROM source WHERE id > 0",
            query_label=None,
        )
        is None
    )
    assert (
        estimate._estimate_clickhouse_source_rows(
            connection,
            "SELECT * FROM source",
            query_label=None,
        )
        == 1
    )
    assert len(queries) == 1
    assert queries[0].startswith("EXPLAIN ESTIMATE")


def test_fetch_dbapi_first_column_handles_row_shapes_and_closes_cursor() -> None:
    cursor = FakeCursor([(1, "ignored"), [], [2], 3])

    values = estimate._fetch_dbapi_first_column(FakeDbapiConnection([cursor]), "EXPLAIN")

    assert values == [1, 2, 3]
    assert cursor.executed == ["EXPLAIN"]
    assert cursor.closed is True


def test_fetch_dbapi_first_column_closes_cursor_after_execute_failure() -> None:
    cursor = FakeCursor([], execute_error=RuntimeError("execute failed"))

    with pytest.raises(RuntimeError, match="execute failed"):
        estimate._fetch_dbapi_first_column(FakeDbapiConnection([cursor]), "EXPLAIN")
    assert cursor.closed is True


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([[{"Plan": {"PlanRows": "4.6"}}]], 5),
        ([{"plan_rows": 8}], 8),
        ([["not-a-plan"]], None),
        ([{"Plan": "invalid"}], None),
    ],
)
def test_extract_gp_json_plan_rows(values: list[Any], expected: int | None) -> None:
    assert estimate._extract_gp_json_plan_rows(values) == expected


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"outputRowCount": 1}, 1),
        ({"plan": {"outputRowCount": {"value": "2.2"}}}, 2),
        ({"statsAndCosts": {"node": {"outputRowCount": 3}}}, 3),
        ({"statsAndCosts": {"node": "invalid"}}, None),
        ({"root": "invalid"}, None),
        ([], None),
    ],
)
def test_trino_output_row_count_from_payload(payload: Any, expected: int | None) -> None:
    assert estimate._trino_output_row_count_from_payload(payload) == expected


def test_source_estimate_scans_later_nested_and_text_candidates() -> None:
    assert (
        estimate._trino_output_row_count_from_payload(
            {
                "statsAndCosts": {
                    "first": {"other": 1},
                    "second": {"outputRowCount": "8.4"},
                }
            }
        )
        == 8
    )
    assert estimate._extract_text_plan_rows([f"rows={'9' * 400}", "rows=3.6"]) == 4
    assert estimate._is_simple_clickhouse_select("DELETE FROM source") is False


def test_extract_trino_output_row_count_scans_multiple_payloads() -> None:
    values = ["not-json", '{"root": {}}', b'{"stats": {"outputRowCount": 6}}']

    assert estimate._extract_trino_output_row_count(values) == 6
    assert estimate._extract_trino_output_row_count(["invalid"]) is None


def test_json_payloads_accepts_structures_strings_and_bytes() -> None:
    structure = {"already": "parsed"}

    assert estimate._json_payloads(
        [structure, [1], b'{"bytes": true}', '  {"text": 2}  ', "", "invalid", 7]
    ) == [structure, [1], {"bytes": True}, {"text": 2}]


def test_extract_text_plan_rows_skips_non_matching_and_invalid_counts() -> None:
    assert estimate._extract_text_plan_rows(["cost only", "rows=nan", "rows=5.6"]) == 6
    assert estimate._extract_text_plan_rows(["no estimate"]) is None


@pytest.mark.parametrize(
    ("source_sql", "expected"),
    [
        ("SELECT value FROM source", True),
        ("SELECT (", False),
        ("SELECT 1", False),
        ("WITH x AS (SELECT 1) SELECT * FROM source", False),
        ("SELECT * FROM source JOIN other ON source.id = other.id", False),
        ("SELECT * FROM (SELECT * FROM source)", False),
        ("SELECT * FROM source WHERE id > 0", False),
        ("SELECT DISTINCT value FROM source", False),
        ("SELECT * FROM source LIMIT 1", False),
    ],
)
def test_is_simple_clickhouse_select(source_sql: str, expected: bool) -> None:
    assert estimate._is_simple_clickhouse_select(source_sql) is expected


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (SimpleNamespace(result_rows=[], column_names=[]), None),
        (SimpleNamespace(result_rows=[(1,), ("2.2",)], column_names=["ROWS"]), 3),
        (SimpleNamespace(result_rows=[("a", "b", "c", 4)], column_names=[]), 4),
        (SimpleNamespace(result_rows=[(5,)], column_names=[]), 5),
        (SimpleNamespace(result_rows=[(1, 2)], column_names=[]), None),
        (SimpleNamespace(result_rows=[("bad",), object()], column_names=["rows"]), None),
    ],
)
def test_extract_clickhouse_estimate_rows(result: Any, expected: int | None) -> None:
    assert estimate._extract_clickhouse_estimate_rows(result) == expected


def test_extract_clickhouse_estimate_rows_skips_short_rows() -> None:
    result = SimpleNamespace(result_rows=[(), (1, 2)], column_names=["a", "rows"])

    assert estimate._extract_clickhouse_estimate_rows(result) == 2


def test_value_from_keys_uses_first_valid_value() -> None:
    assert (
        estimate._value_from_keys({"first": "bad", "second": 7}, ("missing", "first", "second"))
        == 7
    )
    assert estimate._value_from_keys({}, ("missing",)) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, None),
        ({"value": "4.6"}, 5),
        ({"other": 1}, None),
        (3, 3),
        (-1, None),
        (b"2.2", 2),
        ("", None),
        ("unknown", None),
        ("-infinity", None),
        (object(), None),
        (math.inf, None),
        (-0.1, None),
        (2.6, 3),
    ],
)
def test_coerce_estimated_row_count(value: Any, expected: int | None) -> None:
    assert estimate._coerce_estimated_row_count(value) == expected
