"""Acknowledged incoming Hive stages remain independent of final rebuild ownership."""

from __future__ import annotations

from copy import deepcopy

import pytest

from tests.sql._support.parquet_ownership import install, transfer


@pytest.mark.parametrize("keyed", [False, True])
def test_public_parquet_upsert_cleans_both_incoming_hive_and_final_rebuild_stages(
    monkeypatch, write_sql_connections, keyed
):
    database = install(monkeypatch, write_sql_connections)
    assert transfer(keyed=keyed) == 2
    assert database.incoming
    assert set(database.tables) == {("sandbox", "events")}
    assert sorted(database.target.rows, key=lambda row: (row["part"], row["id"])) == [
        {"id": 1, "part": 202401, "payload": 100},
        {"id": 2, "part": 202401, "payload": 200},
        {"id": 9, "part": 202401, "payload": 90},
        {"id": 1, "part": 202402, "payload": 20},
    ]
    state = database.stage_states[-1]
    assert (state.stage_table, state.stage_table) in state.owned_stage_resources
    assert state.final_upsert_stage_table is not None
    assert (
        state.final_upsert_stage_table,
        state.final_upsert_stage_table,
    ) in state.owned_stage_resources
    assert database.remote_removed == [state.stage_external_location]


@pytest.mark.parametrize("keyed", [False, True])
def test_public_lost_hive_create_ack_retains_and_reports_candidate_before_submission(
    monkeypatch, write_sql_connections, capsys, keyed
):
    database = install(monkeypatch, write_sql_connections, lost_creates=1)
    original = deepcopy(database.target.rows)
    with pytest.raises(ConnectionError) as caught:
        transfer(keyed=keyed)
    name, identity = database.incoming[0]
    state = database.stage_states[0]
    visible_name, visible_location, visible_candidates, owned_before = database.before_create[0]
    assert visible_name == name
    assert visible_location
    assert visible_location.startswith("s3://run-owned/")
    assert visible_candidates == [name]
    assert owned_before == []
    assert state.owned_stage_resources == []
    assert not state.stage_table_created
    assert getattr(caught.value, "analytics_toolkit_sql_stage_candidate", None) == name
    assert (
        getattr(caught.value, "analytics_toolkit_sql_stage_external_location", None)
        == visible_location
    )
    assert identity in database.tables
    assert database.target.rows == original
    assert database.remote_removed == []
    output = capsys.readouterr().out
    assert name in output
    assert visible_location in output


def test_successful_retry_still_reports_the_unacknowledged_hive_candidate(
    monkeypatch, write_sql_connections, capsys
):
    database = install(monkeypatch, write_sql_connections, lost_creates=1)
    assert transfer(attempts=2) == 2
    first, identity = database.incoming[0]
    assert identity in database.tables
    assert database.tables[identity].rows == []
    assert all(other not in database.tables for _name, other in database.incoming[1:])
    retained_location = database.before_create[0][1]
    assert retained_location not in database.remote_removed
    output = capsys.readouterr().out
    assert first in output
    assert retained_location in output


def test_confirmed_hive_create_race_never_owns_or_drops_the_foreign_hundred_rows(
    monkeypatch, write_sql_connections, capsys
):
    database = install(monkeypatch, write_sql_connections, race_creates=1)
    assert transfer() == 2
    name, identity, original, location = database.foreign_collisions[0]
    assert len(original) == 100
    assert database.tables[identity].rows == original
    assert set(database.tables) == {("sandbox", "events"), identity}
    state = database.stage_states[0]
    assert name in state.stage_table_candidates
    assert name not in {resource for resource, _stage in state.owned_stage_resources}
    assert location not in database.remote_removed
    assert database.files[location] == original
    assert all(len(table[1].encode()) <= 62 for _name, table in database.incoming)
    assert "Retaining unacknowledged Parquet stage" not in capsys.readouterr().out


def test_public_failed_hive_drop_preserves_the_referenced_parquet_files(
    monkeypatch, write_sql_connections, capsys
):
    database = install(monkeypatch, write_sql_connections)
    failure = ConnectionError("The acknowledged Hive stage could not be dropped")
    database.hive_drop_failure = failure
    with pytest.raises(ConnectionError) as caught:
        transfer()
    assert caught.value is failure
    _name, identity = database.incoming[0]
    location = database.locations[identity]
    assert identity in database.tables
    assert location in database.files
    assert database.files[location] == database.source_rows
    assert database.remote_removed == []
    assert len(database.target.rows) == 4
    assert sum(row["payload"] for row in database.target.rows) == 410
    assert getattr(caught.value, "analytics_toolkit_sql_stage_external_location", None) == location
    output = capsys.readouterr().out
    assert "Retaining Parquet files" in output
    assert location in output
