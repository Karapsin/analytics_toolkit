from __future__ import annotations

from copy import deepcopy

from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter

from tests.sql._support.backend_helpers import (
    Any,
    RecordingConnection,
    SimpleNamespace,
    ch_metadata,
    ch_operations,
    gp_adapter_module,
    gp_ddl,
    gp_insert,
    pd,
    pytest,
)
from tests.sql._support.ch_drop_scope import install_drop_scope


@pytest.mark.parametrize(
    ("engine_full", "database", "expected"),
    [
        (None, "analytics", None),
        ("MergeTree()", "analytics", None),
        ("Distributed('core', 'analytics')", "analytics", None),
        ("Distributed('', 'analytics', 'events')", "analytics", None),
        ("Distributed('core', currentDatabase(), 'events')", None, None),
        ("Distributed('core', currentDatabase(), 'events')", "", None),
    ],
)
def test_clickhouse_distributed_engine_invalid_forms(
    engine_full: object,
    database: object,
    expected: None,
) -> None:
    assert ch_metadata.extract_clickhouse_distributed_shard_table(engine_full, database) is expected


@pytest.mark.parametrize(
    "case",
    [
        ("analytics.events_shard", False, True, "coordinator", "analytics.events_shard"),
        ("analytics.events", False, True, "coordinator", "analytics.events"),
        ("analytics.events", True, False, "remote", "analytics.events_shard"),
    ],
)
def test_clickhouse_operations_dispatches_missing_modes(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Any,
    case: tuple[str, bool, bool, str, str],
) -> None:
    table, shard, facade, selected_host, selected_table = case
    state = install_drop_scope(monkeypatch, write_sql_connections)
    before = state.snapshot()
    expected = deepcopy(before)
    expected[selected_host].pop(selected_table)
    connection = sql.get_sql_connection("ch")
    try:
        ch_operations.drop_table_with_options(
            get_backend_adapter("ch"),
            connection,
            table,
            connection_key="ch",
            ch_cluster="storage",
            ch_drop_shard=shard,
            ch_drop_distributed=facade,
        )
        assert state.snapshot() == expected
        assert len(state.commands) == 1
    finally:
        connection.close()
    assert all(client.closed and client.close_calls == 1 for client in state.opened)
    with pytest.raises(ValueError, match="At least one"):
        ch_operations.drop_table_with_options(
            object(),
            object(),
            "analytics.events",
            connection_key="ch",
            ch_drop_shard=False,
            ch_drop_distributed=False,
        )


def test_gp_ddl_missing_columns_and_partition_fallbacks() -> None:
    relation = pd.DataFrame([{"oid": "1", "schema_name": "public", "relation_name": "empty"}])

    def read_empty_columns(_connection_key: str, query: str) -> pd.DataFrame:
        if "FROM pg_catalog.pg_class AS c" in query:
            return relation
        if "FROM pg_catalog.pg_attribute AS a" in query:
            return pd.DataFrame()
        raise AssertionError(query)

    with pytest.raises(ValueError, match="No columns"):
        gp_ddl.extract_greenplum_catalog_ddl(
            "gp",
            "public.empty",
            read_sql=read_empty_columns,
        )

    assert (
        gp_ddl.read_gp_partition_clause(
            "gp",
            "1",
            read_sql=lambda *_args: pd.DataFrame(),
        )
        == ""
    )

    def legacy_partition(_connection_key: str, query: str) -> pd.DataFrame:
        if "has_partkeydef" in query:
            return pd.DataFrame([{"has_partkeydef": False, "has_partition_def": True}])
        return pd.DataFrame([{"partition_def": "PARTITION BY LIST (region);"}])

    assert (
        gp_ddl.read_gp_partition_clause(
            "gp",
            "1",
            read_sql=legacy_partition,
        )
        == "PARTITION BY LIST (region)"
    )
    assert gp_ddl.format_gp_partition_clause(" ; ") == ""


def test_gp_insert_rows_rolls_back_and_validates_chunk_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = SimpleNamespace(
        build_dataframe_batch_insert_sql=lambda *_args, **_kwargs: "INSERT VALUES %s"
    )
    connection = RecordingConnection()
    monkeypatch.setattr(
        gp_insert,
        "execute_values",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("insert failed")),
    )

    with pytest.raises(RuntimeError, match="insert failed"):
        gp_insert.insert_rows(adapter, connection, "target", ["id"], [[1]])
    assert connection.rollbacks == 1
    assert connection.cursor_instance.closed is True
    assert gp_insert.get_insert_chunk_size(None) == gp_insert.DEFAULT_GP_INSERT_CHUNK_SIZE
    with pytest.raises(ValueError, match="positive integer"):
        gp_insert.get_insert_chunk_size(0)


def test_greenplum_partition_column_validation() -> None:
    assert gp_adapter_module._normalize_gp_partition_column(["event_date"]) == "event_date"
    with pytest.raises(ValueError, match="exactly one"):
        gp_adapter_module._normalize_gp_partition_column(["a", "b"])
