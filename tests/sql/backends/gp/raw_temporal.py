"""Native ClickHouse raw instants bind through the real psycopg adaptation path."""

import datetime as dt

import pandas as pd
import pytest
from analytics_toolkit.sql.backends import get_backend
from analytics_toolkit.sql.backends.gp import insert
from analytics_toolkit.sql.backends.type_resolution import ResolvedColumn
from analytics_toolkit.sql.core.exact_values import ExactTemporal
from psycopg2.extensions import adapt

from tests.sql._support.gp_arrays import AdaptedConnection


def test_raw_instants_adapt_exactly_as_utc_scalar_and_nested_timestamp_arrays():
    raw = ExactTemporal(1704067200123456000, 9, "Europe/Moscow", offset_naive=True)
    expected = dt.datetime(2024, 1, 1, microsecond=123456, tzinfo=dt.timezone.utc)
    assert insert._normalize_uuid_scalar(raw) == expected
    assert (
        adapt(insert._normalize_uuid_scalar(raw)).getquoted()
        == b"'2024-01-01T00:00:00.123456+00:00'::timestamptz"
    )
    adapter = get_backend("gp")
    connection = AdaptedConnection()
    adapter.insert_dataframe_batch(
        connection,
        '"sandbox"."raw_times"',
        pd.DataFrame(
            {
                "value": pd.Series([raw], dtype=object),
                "nested": pd.Series([[[raw, None]]], dtype=object),
            }
        ),
        target_column_types={
            "value": "TIMESTAMP WITH TIME ZONE",
            "nested": "TIMESTAMP WITH TIME ZONE[][]",
        },
        trino_insert_chunk_size=None,
        gp_insert_chunk_size=10,
        connection_type="gp",
        query_label=None,
        on_progress=None,
    )
    statement = connection.target.statements[0]
    assert statement.count(b"'2024-01-01T00:00:00.123456+00:00'::timestamptz") == 2
    assert b"TIMESTAMP WITH TIME ZONE[][]" in statement
    assert connection.committed
    assert insert._normalize_nested_scalar((raw, [raw])) == (expected, [expected])


def test_raw_submicroseconds_never_silently_truncate_in_gp_binding():
    raw = ExactTemporal(1704067200123456789, 9, "UTC")
    with pytest.raises(ValueError, match="microsecond precision"):
        insert._normalize_uuid_scalar(raw)
    narrowed = ResolvedColumn("TIMESTAMP(6) WITH TIME ZONE", target_backend="gp").convert(raw)
    assert narrowed == dt.datetime(2024, 1, 1, microsecond=123456, tzinfo=dt.timezone.utc)
    assert (
        adapt(insert._normalize_uuid_scalar(narrowed)).getquoted()
        == b"'2024-01-01T00:00:00.123456+00:00'::timestamptz"
    )
