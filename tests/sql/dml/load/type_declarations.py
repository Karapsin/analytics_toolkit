"""Authoritative Trino declarations are certified before existing rows can change."""

from __future__ import annotations

import datetime as dt
from copy import deepcopy
from decimal import Decimal

import pandas as pd
import pytest
from analytics_toolkit import sql

from tests.sql._support.trino_time_values import native_time_target


@pytest.mark.parametrize(
    ("existing", "declared", "stored"),
    [
        ("TIMESTAMP(6)", "TIMESTAMP(13)", dt.datetime.fromisoformat("2024-01-01T00:00:00")),
        ("ARRAY(BIGINT)", "ARRAY()", [7]),
        ("ARRAY(BIGINT)", "ARRAY(opaque_type)", [7]),
        ("ARRAY(BIGINT)", "ARRAY(BIGINT, VARCHAR)", [7]),
        ("MAP(VARCHAR, BIGINT)", "MAP()", {"sentinel": 7}),
        ("MAP(VARCHAR, BIGINT)", "MAP(VARCHAR)", {"sentinel": 7}),
        ("DECIMAL(10,0)", "DECIMAL(\uff13\uff18,0)", Decimal(7)),
        ("VARCHAR(10)", "VARCHAR(1,2)", "sentinel"),
    ],
    ids=[
        "timestamp-precision",
        "array-empty-declaration",
        "array-unknown-leaf",
        "array-extra-child",
        "map-empty-declaration",
        "map-missing-child",
        "decimal-nonascii-precision",
        "varchar-extra-parameter",
    ],
)
def test_invalid_declared_type_with_null_input_preserves_live_target(
    monkeypatch, existing, declared, stored
):
    database = native_time_target(monkeypatch, existing)
    database.table_rows[database.target] = [stored]
    before = deepcopy(database.rows)
    incoming = pd.DataFrame({"payload": pd.Series([None], dtype=object)})
    original = incoming.copy(deep=True)

    with pytest.raises(ValueError, match=r"declaration|type|schema|precision|parameter|arity"):
        sql.load_df(
            "trino",
            "iceberg.sandbox.target",
            incoming,
            table_schema={"payload": declared},
            write_mode="truncate_insert",
            retry_cnt=1,
            timeout_increment=0,
        )

    assert database.rows == before
    assert database.native_type == existing
    assert database.mutations == []
    assert database.statements == []
    assert all(connection.close_calls == 1 for connection in database.opened)
    pd.testing.assert_frame_equal(incoming, original)
