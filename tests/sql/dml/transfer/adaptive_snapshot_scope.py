"""A native adaptive checkpoint freezes one complete selected source scope."""

from __future__ import annotations

from copy import deepcopy

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.dml.transfer.flow import adaptive_input, staged_attempt

from tests.sql._support.adaptive_snapshot_scope import install_snapshot_scope


def _transfer():
    return sql.transfer(
        from_db="ch",
        to_db="gp",
        from_sql="SELECT id FROM db.events WHERE {id}",
        transfer_keys="id",
        transfer_key_values=[1, 2],
        to_table="db.target",
        write_mode="append",
        ignore_source_staging=False,
        validate_row_count=True,
        ch_unavailable_shards="healthy_shards",
        return_metadata=True,
        batch_size=10,
        min_batch_size=1,
        max_batch_size=10,
        adaptive_batch_size=False,
        read_concurrency=1,
        write_concurrency=1,
        retry_cnt=1,
        full_retry_cnt=1,
        timeout_increment=0,
        full_timeout_increment=0,
    )


def _record_checkpoints(monkeypatch):
    spools = []
    certifications = []
    options = []
    real_spool = adaptive_input.ReplaySpool
    real_materialize = staged_attempt._materialize_snapshot

    class RecordedSpool(real_spool):
        def certify(self):
            super().certify()
            certifications.append(self)

    def spool(*args, **kwargs):
        result = RecordedSpool(*args, **kwargs)
        spools.append(result)
        return result

    def materialize(current_options, *args, **kwargs):
        options.append(current_options)
        return real_materialize(current_options, *args, **kwargs)

    monkeypatch.setattr(adaptive_input, "ReplaySpool", spool)
    monkeypatch.setattr(staged_attempt, "_materialize_snapshot", materialize)
    return spools, certifications, options


def test_public_adaptive_keyed_snapshot_rejects_scope_drift_before_target_mutation(
    monkeypatch, write_sql_connections
):
    cluster, target = install_snapshot_scope(monkeypatch, write_sql_connections, change_scope=True)
    before = {name: frame.copy(deep=True) for name, frame in target.tables.items()}
    source_before = deepcopy(cluster.tables)
    spools, certifications, options = _record_checkpoints(monkeypatch)

    with pytest.warns(sql.ClickHouseDegradedClusterWarning), pytest.raises(
        ValueError, match="availability scope changed"
    ):
        _transfer()

    assert len(cluster.coverage_changes) == 1
    assert sorted(row[0] for row in cluster.coverage_changes[0]) == [1, 1]
    assert target.commands == []
    assert set(target.tables) == set(before)
    for name, frame in before.items():
        assert target.tables[name].equals(frame)
    assert len(options) == 1
    assert "native_snapshot" not in options[0].input_resolution
    assert "spool" not in options[0].input_resolution
    assert certifications == []
    assert spools
    assert all(not spool.certified for spool in spools)
    assert all(not spool.path.exists() for spool in spools)
    assert not any("__source" in name for _host, name in cluster.tables)
    expected = deepcopy(source_before)
    expected.pop(("h2", "db.events_shard"))
    assert cluster.tables == expected
    assert cluster.clients
    assert all(client.closed for client in cluster.clients)


def test_public_adaptive_keyed_snapshot_accepts_stable_partial_source_scope(
    monkeypatch, write_sql_connections
):
    cluster, target = install_snapshot_scope(monkeypatch, write_sql_connections, change_scope=False)
    cluster.tables.pop(("h2", "db.events_shard"))
    source_before = deepcopy(cluster.tables)
    spools, certifications, options = _record_checkpoints(monkeypatch)

    with pytest.warns(sql.ClickHouseDegradedClusterWarning):
        result = _transfer()

    assert result.rows == 2
    assert result.metadata.expected_source_rows == 2
    assert result.metadata.row_count_validated is True
    assert result.metadata.source_data_complete is False
    assert result.metadata.target_data_complete is True
    assert sorted(target.tables["db.target"].id.tolist()) == [1, 2, 99]
    assert len(options) == 1
    assert "native_snapshot" not in options[0].input_resolution
    assert "spool" not in options[0].input_resolution
    assert certifications == spools
    assert spools
    assert all(not spool.certified for spool in spools)
    assert all(not spool.path.exists() for spool in spools)
    assert sorted(row[0] for _table, rows in cluster.populations for row in rows) == [1, 2]
    assert cluster.coverage_changes == []
    assert cluster.tables == source_before
    assert cluster.clients
    assert all(client.closed for client in cluster.clients)
