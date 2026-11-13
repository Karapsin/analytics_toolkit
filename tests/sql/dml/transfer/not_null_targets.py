"""Transfer stages cannot publish invalid NULLs after clearing live targets."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy

import pytest
from analytics_toolkit import sql

from tests.conftest import DEFAULT_SQL_CONNECTIONS
from tests.sql._support.transfer_not_null import install_transfer_database


def transfer(backend, **options):
    return sql.transfer(
        "ch",
        backend,
        from_sql=options.pop("from_sql", "SELECT id, payload FROM source_rows"),
        to_table="sandbox.events",
        write_mode=options.pop("write_mode", "truncate_insert"),
        retry_cnt=1,
        timeout_increment=0,
        full_retry_cnt=1,
        full_timeout_increment=0,
        batch_size=1,
        min_batch_size=1,
        adaptive_batch_size=False,
        ignore_source_staging=True,
        validate_row_count=False,
        **({"trino_mode": "values"} if backend == "trino" else {}),
        **options,
    )


@pytest.mark.parametrize("backend", ["gp", "trino"])
def test_existing_not_null_transfer_rejects_before_clearing_seed_rows(monkeypatch, backend):
    database = install_transfer_database(monkeypatch, backend)
    original_rows = deepcopy(database.target.rows)
    failure = None
    try:
        transfer(backend)
    except ValueError as error:
        failure = error

    assert database.target.rows == original_rows
    assert database.live_mutations == []
    assert isinstance(failure, ValueError)
    assert len(database.reads) == 1
    assert set(database.tables) == {("sandbox", "events")}


@pytest.mark.parametrize("backend", ["gp", "trino"])
def test_late_key_null_rejects_all_private_stages_before_any_live_publication(monkeypatch, backend):
    database = install_transfer_database(monkeypatch, backend, synchronize_writers=True)
    original_rows = deepcopy(database.target.rows)

    with pytest.raises(ValueError, match="NOT NULL"):
        transfer(
            backend,
            from_sql="SELECT id, payload FROM source_rows WHERE {id}",
            transfer_keys="id",
            transfer_key_values=[7, 8],
            read_concurrency=2,
            write_concurrency=2,
            soft_concurrency_cap=5,
            hard_concurrency_cap=5,
        )

    assert database.target.rows == original_rows
    assert database.live_mutations == []
    assert len(database.reads) == len(set(database.reads)) == 2
    assert sorted(row for _table, rows in database.stage_values for row in rows) == [
        (7, 11),
        (8, None),
    ]
    assert len({table for table, _rows in database.stage_values}) == 2
    assert any(
        "payload" in columns
        and Counter(row[columns.index("payload")] for row in rows) == Counter([11, None])
        for columns, rows in database.constraint_probes
    )
    assert set(database.tables) == {("sandbox", "events")}


def test_partition_upsert_null_rejects_before_any_partition_mutation(
    monkeypatch, write_sql_connections
):
    connections = deepcopy(DEFAULT_SQL_CONNECTIONS)
    connections["trino"]["upsert_partition_drop_sql_template"] = (
        "DELETE FROM {table} WHERE {partition_column} = {partition_value}"
    )
    write_sql_connections(connections)
    database = install_transfer_database(
        monkeypatch,
        "trino",
        columns=("id", "part", "payload"),
        rows=[(7, 202401, 11), (8, 202401, None)],
    )
    database.target.rows = [
        {"id": 7, "part": 202401, "payload": 70},
        {"id": 9, "part": 202401, "payload": 90},
        {"id": 1, "part": 202402, "payload": 10},
    ]
    original_rows = deepcopy(database.target.rows)

    with pytest.raises(ValueError, match="NOT NULL"):
        transfer(
            "trino",
            from_sql="SELECT id, part, payload FROM source_rows",
            write_mode="upsert",
            key_columns=["id"],
            upsert_partition_column="part",
        )

    assert database.target.rows == original_rows
    assert database.live_mutations == []
    assert len(database.reads) == 1
    assert set(database.tables) == {("sandbox", "events")}


@pytest.mark.parametrize("backend", ["gp", "trino"])
def test_omitted_required_transfer_column_rejects_before_clearing_target(monkeypatch, backend):
    database = install_transfer_database(monkeypatch, backend, source_columns=("id",), rows=[(7,)])
    original_rows = deepcopy(database.target.rows)

    with pytest.raises(ValueError, match="required NOT NULL"):
        transfer(backend, from_sql="SELECT id FROM source_rows")

    assert database.target.rows == original_rows
    assert database.live_mutations == []
    assert len(database.reads) == 1
    assert set(database.tables) == {("sandbox", "events")}


@pytest.mark.parametrize("backend", ["gp", "trino"])
def test_nullable_transfer_values_are_published_after_complete_staging(monkeypatch, backend):
    database = install_transfer_database(monkeypatch, backend, not_null=())

    assert transfer(backend) == 2

    assert database.target.rows == [{"id": 7, "payload": 11}, {"id": 8, "payload": None}]
    assert len(database.reads) == 1
    assert len(database.live_mutations) == 2
    assert set(database.tables) == {("sandbox", "events")}


@pytest.mark.parametrize("backend", ["gp", "trino"])
def test_omitted_nullable_transfer_values_keep_subset_insert_semantics(monkeypatch, backend):
    database = install_transfer_database(
        monkeypatch, backend, not_null=(), source_columns=("id",), rows=[(7,)]
    )

    assert transfer(backend, from_sql="SELECT id FROM source_rows") == 1

    assert database.target.rows == [{"id": 7, "payload": None}]
    assert len(database.reads) == 1
    assert set(database.tables) == {("sandbox", "events")}


def test_omitted_greenplum_not_null_default_is_populated_by_live_insert(monkeypatch):
    database = install_transfer_database(
        monkeypatch, "gp", source_columns=("id",), rows=[(7,)], defaults={"payload": 17}
    )

    assert transfer("gp", from_sql="SELECT id FROM source_rows") == 1

    assert database.target.rows == [{"id": 7, "payload": 17}]
    assert len(database.reads) == 1
    assert set(database.tables) == {("sandbox", "events")}


@pytest.mark.parametrize("backend", ["gp", "trino"])
def test_empty_transfer_keep_policy_preserves_target_without_required_omission_error(
    monkeypatch, backend
):
    database = install_transfer_database(monkeypatch, backend, source_columns=("id",), rows=[])
    original_rows = deepcopy(database.target.rows)

    assert transfer(backend, from_sql="SELECT id FROM source_rows", empty_source_policy="keep") == 0

    assert database.target.rows == original_rows
    assert database.live_mutations == []
    assert len(database.reads) == 1
    assert set(database.tables) == {("sandbox", "events")}


@pytest.mark.parametrize("backend", ["gp", "trino"])
def test_native_array_null_elements_do_not_make_the_transfer_column_null(monkeypatch, backend):
    database = install_transfer_database(
        monkeypatch,
        backend,
        rows=[(7, [None, 9])],
        source_types={"id": "Nullable(Int64)", "payload": "Array(Nullable(Int64))"},
        target_types={
            "id": "BIGINT",
            "payload": "BIGINT[]" if backend == "gp" else "ARRAY(BIGINT)",
        },
    )

    assert transfer(backend) == 1

    assert database.target.rows == [{"id": 7, "payload": [None, 9]}]
    assert len(database.reads) == 1
    assert set(database.tables) == {("sandbox", "events")}


@pytest.mark.parametrize("backend", ["gp", "trino"])
def test_null_looking_transfer_text_is_a_nonnull_native_value(monkeypatch, backend):
    database = install_transfer_database(
        monkeypatch,
        backend,
        rows=[(7, "NULL")],
        source_types={"id": "Nullable(Int64)", "payload": "Nullable(String)"},
        target_types={"id": "BIGINT", "payload": "TEXT" if backend == "gp" else "VARCHAR"},
    )

    assert transfer(backend) == 1

    assert database.target.rows == [{"id": 7, "payload": "NULL"}]
    assert len(database.reads) == 1
    assert set(database.tables) == {("sandbox", "events")}
