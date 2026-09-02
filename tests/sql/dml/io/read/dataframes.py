from __future__ import annotations

import importlib
from decimal import Decimal
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pandas as pd
from analytics_toolkit.sql.backends.models import ReadColumnResult
from analytics_toolkit.sql.dml.io.dataframes import (
    column_result_from_rows,
    dataframe_from_column_result,
)

from tests.sql._support.fakes import FakeDbapiConnection

if TYPE_CHECKING:
    import pytest

execute_read_module = importlib.import_module("analytics_toolkit.sql.dml.io.execute_read")
read_sql_module = importlib.import_module("analytics_toolkit.sql.dml.io.read_sql")
sql_module = importlib.import_module("analytics_toolkit.sql")


def test_dataframe_infers_modern_nullable_dtypes_from_raw_columns() -> None:
    large_integer = 2**53 + 1
    result = column_result_from_rows(
        ("id", "flag", "score", "label", "amount"),
        [
            (large_integer, True, 1.0, "one", Decimal("12.3400")),
            (None, None, None, None, None),
        ],
    )

    dataframe = dataframe_from_column_result(result)

    assert dataframe.dtypes.astype(str).tolist() == [
        "Int64",
        "boolean",
        "Float64",
        "string",
        "object",
    ]
    assert int(dataframe.loc[0, "id"]) == large_integer
    assert dataframe.loc[0, "score"] == 1.0
    assert dataframe.loc[0, "amount"] == Decimal("12.3400")
    assert dataframe.iloc[1].isna().all()


def test_dataframe_preserves_unsigned_and_unrepresentable_integers() -> None:
    result = column_result_from_rows(
        ("unsigned", "mixed_range", "too_large"),
        [
            (2**63, -1, 2**64),
            (None, 2**63, None),
        ],
    )

    dataframe = dataframe_from_column_result(result)

    assert dataframe.dtypes.astype(str).tolist() == ["UInt64", "object", "object"]
    assert int(dataframe.loc[0, "unsigned"]) == 2**63
    assert dataframe.loc[1, "mixed_range"] == 2**63
    assert dataframe.loc[0, "too_large"] == 2**64


def test_dataframe_preserves_duplicate_empty_and_columnless_results() -> None:
    duplicate = dataframe_from_column_result(
        column_result_from_rows(("value", "value"), [(1, "one"), (None, None)])
    )
    empty = dataframe_from_column_result(column_result_from_rows(("id",), []))
    columnless = dataframe_from_column_result(ReadColumnResult((), (), 2))

    assert duplicate.columns.tolist() == ["value", "value"]
    assert duplicate.dtypes.astype(str).tolist() == ["Int64", "string"]
    assert empty.shape == (0, 1)
    assert str(empty.dtypes.iloc[0]) == "object"
    assert columnless.shape == (2, 0)


def test_read_dataframe_preserves_nullable_integer_precision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    large_integer = 2**53 + 1
    connection = FakeDbapiConnection(
        rows=[(large_integer,), (None,)],
        description=[("contact_id",)],
    )
    monkeypatch.setattr(read_sql_module, "get_sql_connection", lambda _key: connection)

    dataframe = sql_module.read("gp", "select contact_id", retry_cnt=1)

    assert str(dataframe.dtypes["contact_id"]) == "Int64"
    assert int(dataframe.loc[0, "contact_id"]) == large_integer
    assert dataframe.loc[1, "contact_id"] is pd.NA


def test_clickhouse_read_dataframe_uses_raw_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queries: list[tuple[str, bool]] = []

    class Client:
        def query(self, sql: str, *, column_oriented: bool) -> SimpleNamespace:
            queries.append((sql, column_oriented))
            return SimpleNamespace(
                column_names=("contact_id",),
                result_columns=([2**53 + 1, None],),
                row_count=2,
            )

        def close(self) -> None:
            return None

    monkeypatch.setattr(read_sql_module, "get_sql_connection", lambda _key: Client())

    dataframe = sql_module.read("ch", "select contact_id", retry_cnt=1)

    assert str(dataframe.dtypes["contact_id"]) == "Int64"
    assert int(dataframe.loc[0, "contact_id"]) == 2**53 + 1
    assert queries == [("select contact_id", True)]


def test_execute_read_preserves_nullable_integer_precision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    large_integer = 2**53 + 1
    connection = FakeDbapiConnection(
        rows=[(large_integer,), (None,)],
        description=[("contact_id",)],
    )
    monkeypatch.setattr(execute_read_module, "get_sql_connection", lambda _key: connection)

    dataframe = sql_module.execute_read("trino", "select contact_id", retry_cnt=1)

    assert str(dataframe.dtypes["contact_id"]) == "Int64"
    assert int(dataframe.loc[0, "contact_id"]) == large_integer
    assert dataframe.loc[1, "contact_id"] is pd.NA
