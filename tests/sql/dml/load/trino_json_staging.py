"""Unsupported Hive JSON stages must fail before changing destination rows."""

from __future__ import annotations

from copy import deepcopy

import pandas as pd
import pytest
from analytics_toolkit import sql

from tests.sql._support.load_table import _write_trino_connections
from tests.sql._support.trino_time_values import native_time_target


@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
@pytest.mark.parametrize(
    ("native", "value"),
    [
        ("JSON", "document text"),
        ("JSON", {"key": 1}),
        ("JSON", None),
        ("ARRAY(JSON)", ["document", None]),
        ("MAP(VARCHAR, JSON)", {"key": "document"}),
        ('ROW("quoted field" ARRAY(JSON))', (["document"],)),
    ],
)
def test_json_hive_staging_rejects_before_destination_mutation(
    monkeypatch, write_sql_connections, explicit, native, value
):
    _write_trino_connections(
        write_sql_connections,
        s3_transfer_staging_location="memory://unit-test-stage/json",
    )
    database = native_time_target(monkeypatch, native)
    original = deepcopy(database.rows)
    frame = pd.DataFrame({"payload": pd.Series([value], dtype=object)})
    options = {"table_schema": {"payload": native}} if explicit else {}

    with pytest.raises(ValueError, match="Parquet stage type"):
        sql.load_df(
            "trino_stage",
            "iceberg.sandbox.target",
            frame,
            write_mode="truncate_insert",
            retry_cnt=1,
            timeout_increment=0,
            **options,
        )

    assert database.rows == original
    assert database.mutations == []
    assert database.statements == []
    assert all(connection.close_calls == 1 for connection in database.opened)
