"""Transfer mutation attempts retain only their own successful finalization count."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from analytics_toolkit.sql.backends.finalization_outcome import record_final_target_rows
from analytics_toolkit.sql.dml.transfer.flow import finalize, row_counts
from analytics_toolkit.sql.dml.transfer.runtime.models import TransferOptions, TransferStageState


def _options(*, final_target_rows=None):
    return TransferOptions(
        from_db_key="trino",
        from_db_backend="trino",
        to_db_key="ch",
        to_db_backend="ch",
        source_sql="SELECT id FROM sandbox.source",
        target_table="sandbox.target",
        write_mode="append",
        replace_target_table=False,
        final_target_rows=final_target_rows,
    )


def _finalize(options, connection):
    state = TransferStageState(
        target_exists=True,
        target_existed_at_start=True,
        stage_table="sandbox.stage",
        stage_column_types={"id": "Int64"},
    )

    def run_target(role, operation):
        assert role == "finalize_target"
        return operation({"connection": connection})

    finalize._finalize_target_once(
        options,
        state,
        "sandbox.stage",
        {"id": "Int64"},
        target_precleared=False,
        fresh_clickhouse_target=False,
        target_connection_runner=run_target,
    )


def test_finalization_zero_then_unknown_on_same_options_cannot_reuse_stale_count(monkeypatch):
    options = _options()
    connection = object()
    mutation_entries = []

    def finalize_target(backend, actual_connection, **kwargs):
        assert backend == "ch"
        assert actual_connection is connection
        assert kwargs["target_table"] == "sandbox.target"
        assert kwargs["stage_table"] == "sandbox.stage"
        mutation_entries.append(options.final_target_rows)
        if len(mutation_entries) == 1:
            record_final_target_rows(0)
            assert options.final_target_rows is None

    monkeypatch.setattr(finalize, "finalize_stage_table", finalize_target)

    _finalize(options, connection)
    assert options.final_target_rows == 0
    _finalize(options, connection)

    assert mutation_entries == [None, None]
    assert options.final_target_rows is None


def test_failed_finalizer_cannot_publish_a_reported_count_or_retain_previous_count(monkeypatch):
    options = _options(final_target_rows=0)
    connection = object()
    primary = RuntimeError("backend failed after reporting its target count")

    def fail_after_reporting(backend, actual_connection, **_kwargs):
        assert backend == "ch"
        assert actual_connection is connection
        assert options.final_target_rows is None
        record_final_target_rows(42)
        assert options.final_target_rows is None
        raise primary

    monkeypatch.setattr(finalize, "finalize_stage_table", fail_after_reporting)

    with pytest.raises(RuntimeError) as failure:
        _finalize(options, connection)

    assert failure.value is primary
    assert options.final_target_rows is None


@pytest.mark.parametrize("known_rows", [0, 42])
def test_known_final_target_count_is_read_without_any_alias_or_sql_access(known_rows):
    options = _options(final_target_rows=known_rows)

    def forbidden(*_args, **_kwargs):
        pytest.fail("A completed finalizer count must not reopen or recount another scope.")

    assert (
        row_counts.best_effort_transfer_target_count(
            options,
            open_connection=forbidden,
            count_rows=forbidden,
            target_connection_runner=forbidden,
        )
        == known_rows
    )
    assert options.final_target_rows == known_rows


@pytest.mark.parametrize("leased", [False, True], ids=["fresh-connection", "leased-target"])
@pytest.mark.parametrize("observed_rows", [0, 42, None], ids=["empty", "populated", "unknown"])
def test_unknown_finalizer_count_keeps_the_existing_best_effort_fallback(leased, observed_rows):
    options = _options()
    events = []
    connection = SimpleNamespace(close=lambda: events.append("close"))

    def open_connection(alias):
        assert not leased, "A leased target count opened another alias connection."
        assert alias == "ch"
        events.append("open")
        return connection

    def count_rows(backend, actual_connection, table, *, query_label):
        assert backend == "ch"
        assert actual_connection is connection
        assert table == "sandbox.target"
        assert query_label is None
        events.append("count")
        if observed_rows is None:
            message = "Target count is unavailable from this connection."
            raise OSError(message)
        return observed_rows

    def run_target(role, operation):
        assert role == "final_target_count"
        events.append("lease")
        return operation({"connection": connection})

    result = row_counts.best_effort_transfer_target_count(
        options,
        open_connection=open_connection,
        count_rows=count_rows,
        target_connection_runner=run_target if leased else None,
    )

    assert result == observed_rows
    assert events == (["lease", "count"] if leased else ["open", "count", "close"])
    assert options.final_target_rows is None
