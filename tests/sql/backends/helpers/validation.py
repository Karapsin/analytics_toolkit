from __future__ import annotations

from tests.sql._support.backend_helpers import (
    Any,
    RecordingConnection,
    SimpleNamespace,
    ch_metadata,
    ch_operations,
    gp_adapter_module,
    gp_ddl,
    gp_insert,
    importlib,
    pd,
    pytest,
)


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


def test_clickhouse_operations_dispatches_missing_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle = importlib.import_module("analytics_toolkit.sql.backends.ch.lifecycle")
    calls: list[tuple[str, str, dict[str, Any]]] = []
    monkeypatch.setattr(
        lifecycle,
        "drop_ch_table",
        lambda _connection, table, **kwargs: calls.append(("table", table, kwargs)),
    )
    monkeypatch.setattr(
        lifecycle,
        "drop_ch_distributed_table_pair",
        lambda _connection, table, **kwargs: calls.append(("pair", table, kwargs)),
    )
    monkeypatch.setattr(
        importlib.import_module("analytics_toolkit.general"),
        "time_print",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(ValueError, match="ch_drop_shard must be True"):
        ch_operations.drop_table_with_options(
            object(),
            object(),
            "analytics.events_shard",
            connection_key="ch",
            ch_drop_shard=False,
        )
    ch_operations.drop_table_with_options(
        object(),
        object(),
        "analytics.events",
        connection_key="ch",
        ch_drop_shard=False,
        ch_drop_distributed=True,
    )
    ch_operations.drop_table_with_options(
        object(),
        object(),
        "analytics.events",
        connection_key="ch",
        ch_drop_shard=True,
        ch_drop_distributed=False,
    )
    assert [call[:2] for call in calls] == [
        ("table", "analytics.events"),
        ("table", "analytics.events_shard"),
    ]
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
