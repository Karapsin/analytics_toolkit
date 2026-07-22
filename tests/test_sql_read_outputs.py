from __future__ import annotations

import importlib
import inspect
from typing import Any, cast

import pandas as pd
import pytest
from tests.sql_fakes import FakeDbapiConnection

read_sql_module = importlib.import_module("analytics_toolkit.sql.dml.io.read_sql")
sql_module = importlib.import_module("analytics_toolkit.sql")


class ColumnClickHouseResult:
    def __init__(
        self,
        column_names: tuple[str, ...],
        columns: tuple[list[Any], ...],
    ) -> None:
        self.column_names = column_names
        self.result_columns = columns
        self.row_count = len(next(iter(columns), []))


class ColumnClickHouseClient:
    def __init__(self, result: ColumnClickHouseResult) -> None:
        self.result = result
        self.query_calls: list[tuple[str, dict[str, Any]]] = []
        self.query_df_calls = 0
        self.close_calls = 0

    def query(self, sql: str, **kwargs: Any) -> ColumnClickHouseResult:
        self.query_calls.append((sql, kwargs))
        return self.result

    def query_df(self, sql: str) -> pd.DataFrame:
        del sql
        self.query_df_calls += 1
        pytest.fail("Dictionary reads must not call query_df.")

    def close(self) -> None:
        self.close_calls += 1


def test_read_output_type_defaults_to_dataframe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeDbapiConnection(rows=[(1,)], description=[("value",)])
    monkeypatch.setattr(read_sql_module, "get_sql_connection", lambda _key: connection)

    result = sql_module.read("gp", "select 1", retry_cnt=1)

    assert inspect.signature(sql_module.read).parameters["output_type"].default == "df"
    assert isinstance(result, pd.DataFrame)
    assert result.to_dict("list") == {"value": [1]}


def test_read_exports_dataframe_to_excel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeDbapiConnection(rows=[(1,)], description=[("value",)])
    calls: list[tuple[object, bool]] = []
    monkeypatch.setattr(read_sql_module, "get_sql_connection", lambda _key: connection)

    def fake_to_excel(self: pd.DataFrame, path: object, *, index: bool) -> None:
        assert self.to_dict("list") == {"value": [1]}
        calls.append((path, index))

    monkeypatch.setattr(pd.DataFrame, "to_excel", fake_to_excel)

    result = sql_module.read("gp", "select 1", retry_cnt=1, to_excel="example.xlsx")

    assert result.to_dict("list") == {"value": [1]}
    assert calls == [("example.xlsx", False)]


def test_read_rejects_excel_export_for_non_dataframe_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        read_sql_module,
        "get_sql_connection",
        lambda _key: pytest.fail("Unsupported export must fail before connecting."),
    )

    with pytest.raises(read_sql_module.InvalidSqlInputError, match="only supported"):
        sql_module.read(
            "gp",
            "select 1",
            output_type="scalar",
            to_excel="example.xlsx",
        )


@pytest.mark.parametrize(
    ("output_type", "rows", "description", "expected"),
    [
        ("scalar", [(7,)], [("value",)], 7),
        ("list", [(1,), (2,)], [("value",)], [1, 2]),
        ("list", [], [("value",)], []),
    ],
)
def test_read_scalar_and_list_outputs(
    monkeypatch: pytest.MonkeyPatch,
    output_type: str,
    rows: list[tuple[Any, ...]],
    description: list[tuple[Any, ...]],
    expected: Any,
) -> None:
    connection = FakeDbapiConnection(rows=rows, description=description)
    monkeypatch.setattr(read_sql_module, "get_sql_connection", lambda _key: connection)

    result = sql_module.read(
        "gp",
        "select value from source_table",
        retry_cnt=1,
        output_type=cast("Any", output_type),
    )

    assert result == expected


@pytest.mark.parametrize(
    ("output_type", "rows", "description", "message"),
    [
        ("scalar", [], [("value",)], "exactly one row and one column"),
        ("scalar", [(1,), (2,)], [("value",)], "exactly one row and one column"),
        ("scalar", [(1, 2)], [("left",), ("right",)], "exactly one row and one column"),
        ("list", [], [], "exactly one column"),
        ("list", [(1, 2)], [("left",), ("right",)], "exactly one column"),
    ],
)
def test_read_scalar_and_list_validate_result_shape(
    monkeypatch: pytest.MonkeyPatch,
    output_type: str,
    rows: list[tuple[Any, ...]],
    description: list[tuple[Any, ...]],
    message: str,
) -> None:
    connection = FakeDbapiConnection(rows=rows, description=description)
    monkeypatch.setattr(read_sql_module, "get_sql_connection", lambda _key: connection)

    with pytest.raises(read_sql_module.InvalidSqlInputError, match=message):
        sql_module.read(
            "gp",
            "select value from source_table",
            retry_cnt=1,
            output_type=cast("Any", output_type),
        )


def test_read_dict_uses_direct_dbapi_columns_and_preserves_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeDbapiConnection(
        rows=[(1, "one"), (2, "two")],
        description=[("id",), ("label",)],
    )
    monkeypatch.setattr(read_sql_module, "get_sql_connection", lambda _key: connection)
    monkeypatch.setattr(
        read_sql_module,
        "_read_dbapi_query",
        lambda *_args: pytest.fail("Dictionary reads must not use the DataFrame path."),
    )

    result = sql_module.read(
        "trino",
        "select id, label from source_table",
        retry_cnt=1,
        output_type="dict",
        return_metadata=True,
    )

    assert result.data == {"id": [1, 2], "label": ["one", "two"]}
    assert result.rows == 2
    assert result.metadata.read_rows == 2
    assert result.metadata.source_rows == 2


def test_read_dict_preserves_empty_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = FakeDbapiConnection(
        rows=[],
        description=[("id",), ("label",)],
    )
    monkeypatch.setattr(read_sql_module, "get_sql_connection", lambda _key: connection)

    result = sql_module.read("gp", "select id, label", retry_cnt=1, output_type="dict")

    assert result == {"id": [], "label": []}


def test_read_dict_uses_clickhouse_column_oriented_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = ColumnClickHouseClient(
        ColumnClickHouseResult(
            ("id", "label"),
            ([1, 2], ["one", "two"]),
        )
    )
    monkeypatch.setattr(read_sql_module, "get_sql_connection", lambda _key: connection)

    result = sql_module.read(
        "ch",
        "select id, label from source_table",
        retry_cnt=1,
        output_type="dict",
    )

    assert result == {"id": [1, 2], "label": ["one", "two"]}
    assert connection.query_calls == [
        ("select id, label from source_table", {"column_oriented": True})
    ]
    assert connection.query_df_calls == 0
    assert connection.close_calls == 1


def test_read_dict_rejects_duplicate_columns_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeDbapiConnection(
        rows=[(1, 2)],
        description=[("value",), ("value",)],
    )
    connection_calls = 0

    def get_connection(_key: str) -> FakeDbapiConnection:
        nonlocal connection_calls
        connection_calls += 1
        return connection

    monkeypatch.setattr(read_sql_module, "get_sql_connection", get_connection)

    with pytest.raises(read_sql_module.InvalidSqlInputError, match="unique column names"):
        sql_module.read(
            "gp",
            "select left_value as value, right_value as value",
            retry_cnt=3,
            output_type="dict",
        )

    assert connection_calls == 1


def test_read_dict_logs_direct_backend_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeDbapiConnection()
    adapter = read_sql_module.get_backend_adapter("gp")
    monkeypatch.setattr(read_sql_module, "get_sql_connection", lambda _key: connection)

    def fail_read(*_args: Any) -> None:
        message = "direct column read failed"
        raise RuntimeError(message)

    monkeypatch.setattr(adapter, "_read_columns_impl", fail_read)

    with pytest.raises(RuntimeError, match="direct column read failed"):
        sql_module.read("gp", "select 1", retry_cnt=1, output_type="dict")


def test_read_rejects_invalid_output_type_before_opening_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        read_sql_module,
        "get_sql_connection",
        lambda _key: pytest.fail("Invalid output_type must fail before connecting."),
    )

    with pytest.raises(read_sql_module.InvalidSqlInputError, match="Unsupported output_type"):
        sql_module.read(
            "gp",
            "select 1",
            output_type=cast("Any", "records"),
        )
