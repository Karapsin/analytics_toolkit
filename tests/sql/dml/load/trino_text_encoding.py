"""The complete text domain is certified before live clearing or Arrow writes."""

from __future__ import annotations

import pandas as pd
import pyarrow as pa
import pytest
from analytics_toolkit import sql
from sqlglot import exp, parse_one

from tests.sql._support.load_table import _write_trino_connections, load_df_module
from tests.sql._support.trino_time_values import parquet_subset_target


def test_arrow_text_domain_preserves_valid_non_bmp_text_and_rejects_a_lone_surrogate():
    assert pa.array(["valid-\U0001f642", None], type=pa.string()).to_pylist() == [
        "valid-\U0001f642",
        None,
    ]
    with pytest.raises((UnicodeError, pa.ArrowInvalid)):
        pa.array(["\ud800private-selected-text"], type=pa.string())


@pytest.mark.parametrize("native", ["VARCHAR", "ARRAY(VARCHAR)"])
def test_public_parquet_load_rejects_late_unencodable_text_before_clear_or_stage(
    monkeypatch, write_sql_connections, native
):
    _write_trino_connections(
        write_sql_connections, s3_transfer_staging_location="s3://private-test-stage/text"
    )
    database = parquet_subset_target(monkeypatch)
    monkeypatch.setattr(
        load_df_module, "get_table_column_types", lambda *_args, **_kwargs: {"payload": native}
    )
    private = "\ud800private-selected-text"
    values = ["safe", None, private] if native == "VARCHAR" else [["safe"], [], [private]]
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})
    original = frame.copy(deep=True)

    try:
        with pytest.raises(ValueError, match=r"(?i)utf.?8|encod") as error:
            sql.load_df(
                "trino_stage",
                "iceberg.sandbox.target",
                frame,
                table_schema={"payload": native},
                write_mode="truncate_insert",
                retry_cnt=1,
                timeout_increment=0,
            )
    finally:
        mutations = [
            statement
            for statement in database.commands
            if isinstance(
                parse_one(statement, read="trino"),
                (exp.Delete, exp.TruncateTable, exp.Create, exp.Insert, exp.Drop),
            )
        ]
        assert mutations == []
        assert database.writes == []

    assert "private-selected-text" not in str(error.value)
    pd.testing.assert_frame_equal(frame, original)
