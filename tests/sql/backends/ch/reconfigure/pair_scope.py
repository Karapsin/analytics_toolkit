"""An existing pair cannot donate an unrelated table in a desired facade scope."""

from __future__ import annotations

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.cutover import StructuralCutoverError
from analytics_toolkit.sql.backends.ch.outage_client import OutageClient
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError

from tests.sql._support.reconfigure_pair_scope import (
    PairScopeClient,
    configure_pair_scope,
    connect_pair_scope,
)


def _convert_to_defaults(*, dry_run=False):
    return sql.ch_reconfigure_table(
        "ch",
        "analytics.events",
        to_defaults=True,
        retry_cnt=1,
        timeout_increment=0,
        return_metadata=True,
        dry_run=dry_run,
    )


def _conversion_that_must_reject(raw, original, *, dry_run):
    try:
        result = _convert_to_defaults(dry_run=dry_run)
    except StructuralCutoverError as error:
        diagnostic = {
            "failure": str(error),
            "guard": [type(wrapper).__name__ for wrapper in raw.wrappers],
            "commands": list(raw.commands),
            "before": original,
            "after": raw.snapshot(),
        }
        assert raw.snapshot() == original, repr(diagnostic)
        raise
    if dry_run:
        pytest.fail("A conflicting source or destination must fail planning before any DDL")
    coordinator = raw.host_tables[raw.coordinator].get(raw.source_table)
    diagnostic = {
        "guard": [type(wrapper).__name__ for wrapper in raw.wrappers],
        "counts": (
            result.metadata.source_rows,
            result.metadata.staged_rows,
            result.metadata.final_target_rows,
        ),
        "row_count_validated": result.metadata.row_count_validated,
        "cleanup_complete": result.data["cleanup_complete"],
        "foreign_rows_before": len(original[raw.remote][raw.source_table]["rows"]),
        "foreign_rows_after": len(raw.rows(raw.remote, raw.source_table)),
        "foreign_uuid_before": original[raw.remote][raw.source_table]["uuid"],
        "foreign_uuid_after": raw.host_tables[raw.remote][raw.source_table].uuid,
        "commands": list(raw.commands),
        "before": original,
        "after": raw.snapshot(),
        "coordinator_dangling": coordinator is not None
        and coordinator.route is not None
        and all(
            coordinator.route[1] not in raw.host_tables[host]
            for host in raw.clusters[coordinator.route[0]]
        ),
    }
    assert raw.snapshot() == original, repr(diagnostic)
    pytest.fail("An unsafe table reconfiguration must be rejected before any DDL")


@pytest.mark.parametrize("dry_run", [True, False], ids=["plan", "execute"])
def test_pair_scope_defaults_cannot_adopt_remote_logical_storage_under_real_outage_guard(
    monkeypatch, write_sql_connections, dry_run
):
    configure_pair_scope(write_sql_connections, "frontends")
    raw = PairScopeClient()
    original = raw.snapshot()
    connect_pair_scope(monkeypatch, raw)

    with pytest.raises(InvalidSqlInputError, match=r"[Ff]acade|[Ss]cope|[Oo]wn|[Cc]oordinator"):
        _conversion_that_must_reject(raw, original, dry_run=dry_run)

    assert len(raw.wrappers) == 1
    assert isinstance(raw.wrappers[0], OutageClient)
    assert raw.commands == []
    assert raw.snapshot() == original
    assert raw.rows(raw.remote, raw.source_table) == raw.remote_original
    assert raw.rows(raw.coordinator, raw.source_table) == [(1,), (2,), (3,)]
    assert raw.closed


@pytest.mark.parametrize("dry_run", [True, False], ids=["plan", "execute"])
def test_pair_scope_occupied_physical_destination_is_rejected_before_any_source_rename(
    monkeypatch, write_sql_connections, dry_run
):
    configure_pair_scope(write_sql_connections, "coordinator_only")
    raw = PairScopeClient()
    original = raw.snapshot()
    connect_pair_scope(monkeypatch, raw)

    with pytest.raises(
        InvalidSqlInputError, match=r"[Pp]hysical|[Dd]estination|[Oo]ccup|[Uu]nowned"
    ):
        _conversion_that_must_reject(raw, original, dry_run=dry_run)

    assert isinstance(raw.wrappers[0], OutageClient)
    assert raw.commands == []
    assert raw.snapshot() == original
    assert raw.rows(raw.remote, raw.source_table) == raw.remote_original
    assert raw.rows(raw.coordinator, raw.source_table) == [(1,), (2,), (3,)]
    assert raw.closed


def test_pair_scope_defaults_allow_all_actual_managed_facades_with_real_outage_guard(
    monkeypatch, write_sql_connections
):
    configure_pair_scope(write_sql_connections, "core")
    raw = PairScopeClient(managed_remote_facade=True)
    connect_pair_scope(monkeypatch, raw)

    result = _convert_to_defaults()

    assert len(raw.wrappers) == 1
    assert isinstance(raw.wrappers[0], OutageClient)
    assert result.data["strategy"] == "pair_to_local"
    assert result.metadata.source_rows == result.metadata.staged_rows == 3
    assert result.metadata.final_target_rows == 3
    assert result.metadata.row_count_validated is True
    assert raw.rows(raw.remote, raw.source_table) == [(1,), (2,), (3,)]
    assert raw.source_table not in raw.host_tables[raw.coordinator]
    assert all(
        "__facade_backup_" not in name for tables in raw.host_tables.values() for name in tables
    )
