"""Source policy failures retain exact input and stop before publication."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pandas as pd
import pytest
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.dml.destination_context import freeze_destination_timezone
from analytics_toolkit.sql.dml.io.dataframes import dataframe_from_columns
from analytics_toolkit.sql.dml.transfer.io import source
from analytics_toolkit.sql.dml.transfer.io.source import _iter_clickhouse_batches
from analytics_toolkit.sql.dml.transfer.runtime.models import RowBatch


def test_source_timezone_identity_is_frozen_and_unknown_policy_cannot_bind_naive_values():
    adapter = get_backend_adapter("ch")
    source = SimpleNamespace(effective_read_timezone="Europe/Moscow")
    assert adapter.source_read_timezone(source) == "Europe/Moscow"
    batch = RowBatch(["stamp"], [(dt.datetime.fromisoformat("2024-01-01T12:00:00"),)])
    cache = {}
    bound = adapter.normalize_source_temporal_batch(source, batch, {"stamp": "DateTime"}, cache)
    assert bound.rows[0][0].astimezone(dt.timezone.utc).hour == 9
    source.effective_read_timezone = "UTC"
    with pytest.raises(InvalidSqlInputError, match="read timezone changed"):
        adapter.normalize_source_temporal_batch(source, batch, {"stamp": "DateTime"}, cache)
    with pytest.raises(InvalidSqlInputError, match="Cannot resolve the source"):
        adapter.normalize_source_temporal_batch(SimpleNamespace(), batch, {"stamp": "DateTime"}, {})
    assert cache == {"source_timezone": "Europe/Moscow"}


def test_nested_source_timestamps_use_source_policy_and_gp_values_keep_wallclock():
    adapter = get_backend_adapter("ch")
    stamp = dt.datetime.fromisoformat("2024-01-01T12:00:00")
    batch = RowBatch(["events"], [([stamp],)])
    result = adapter.normalize_source_temporal_batch(
        SimpleNamespace(effective_read_timezone="Europe/Moscow"),
        batch,
        {"events": "Array(DateTime64(6))"},
        {},
    )
    assert result.rows[0][0][0].astimezone(dt.timezone.utc).hour == 9
    gp = get_backend_adapter("gp")
    assert gp.source_read_timezone(SimpleNamespace()) is None
    assert gp.normalize_source_temporal_batch(SimpleNamespace(), batch, {}, {}) is batch


def test_write_probe_can_use_returned_policy_without_assuming_a_client_property():
    queries = []
    connection = SimpleNamespace(
        query=lambda query: queries.append(query) or SimpleNamespace(result_rows=[("UTC",)])
    )
    adapter = get_backend_adapter("ch")
    assert adapter.destination_write_timezone(connection, required=True) == "UTC"
    assert len(queries) == 1
    stamp = dt.datetime.fromisoformat("2024-01-01T12:00:00")
    assert adapter.requires_dataframe_timezone(pd.DataFrame({"events": [{"k": [stamp]}]}))
    assert not adapter.requires_dataframe_timezone(pd.DataFrame({"events": [{"k": [42]}]}))


def test_old_timezone_policy_checkpoint_is_rejected_before_any_probe():
    def query(_query):
        pytest.fail("An incompatible checkpoint must fail before connecting")

    with pytest.raises(InvalidSqlInputError, match="timezone policy differs"):
        freeze_destination_timezone(
            {"timezone_policy_version": -1},
            "ch",
            SimpleNamespace(query=query),
            {"stamp": "DateTime"},
        )


class UncoercibleCell:
    def __array__(self, dtype=None, copy=None):
        message = "No NumPy coercion is supported"
        raise ValueError(message)


def test_dataframe_keeps_opaque_values_when_extension_inference_rejects_array_protocol():
    value = UncoercibleCell()
    result = dataframe_from_columns(["opaque"], [[value]], row_count=1)
    assert result.iloc[0, 0] is value
    assert str(result.dtypes.iloc[0]) == "object"


def test_source_exact_mode_is_enabled_before_stream_dataframe_construction():
    events = []

    class Stream:
        def __enter__(self):
            events.append("enter")
            return iter([pd.DataFrame({"id": [42]})])

        def __exit__(self, *_args):
            events.append("close")

    def start(_query):
        assert events == ["exact"]
        return Stream()

    connection = SimpleNamespace(
        enable_exact_source_values=lambda: events.append("exact"), query_df_stream=start
    )
    batches = list(
        _iter_clickhouse_batches(
            "ch", {"connection": connection}, "SELECT 42", lambda: 1, 1, 0, False
        )
    )
    assert batches == [RowBatch(["id"], [(42,)])]
    assert events == ["exact", "enter", "close"]


def test_failed_stream_start_does_not_close_a_context_that_was_never_entered(monkeypatch):

    failures = []

    def fail(*_args, **_kwargs):
        failures.append(True)
        message = "Source stream is unavailable before header"
        raise ConnectionError(message)

    monkeypatch.setattr(source, "_start_clickhouse_stream_with_retry", fail)
    with pytest.raises(ConnectionError, match="before header"):
        list(_iter_clickhouse_batches("ch", {}, "SELECT id", lambda: 1, 1, 0, False))
    assert failures == [True]
