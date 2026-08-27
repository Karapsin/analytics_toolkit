from __future__ import annotations

from tests.sql._support.adapters import (
    Any,
    InvalidSqlInputError,
    MinimalContractAdapter,
    SimpleNamespace,
    StageFinalizationRequest,
    StageTargetTableRequest,
    TargetWriteModeRequest,
    UnsupportedConnectionTypeError,
    adapter_defaults_module,
    backend_common_methods_module,
    get_backend_adapter,
    importlib,
    pd,
    pytest,
)


@pytest.mark.parametrize(
    ("function", "args", "kwargs"),
    [
        (
            adapter_defaults_module.build_show_tables_query,
            (object(), object(), None, None, None),
            {},
        ),
        (
            adapter_defaults_module.extract_table_ddl,
            (object(), "db", "schema.target"),
            {"read_sql": lambda db, sql: None},
        ),
        (
            adapter_defaults_module.build_drop_partitions_sqls,
            (object(), "schema.target", ["2026-01-01"]),
            {},
        ),
        (
            adapter_defaults_module.build_create_partition_sql,
            (object(), "schema.target"),
            {"name": "p1"},
        ),
        (
            adapter_defaults_module.build_vacuum_table_sql,
            (object(), "schema.target"),
            {},
        ),
    ],
)
def test_adapter_abstract_defaults_raise_not_implemented(
    function: Any,
    args: tuple[object, ...],
    kwargs: dict[str, object],
) -> None:
    with pytest.raises(NotImplementedError):
        function(*args, **kwargs)


def test_adapter_default_create_kwargs_include_partition_and_order() -> None:
    adapter = get_backend_adapter("trino")
    common_kwargs = {
        "gp_distributed_by_key": None,
        "partition_by": ["dt"],
        "order_by": ["id"],
        "ch_engine": "ReplicatedMergeTree",
        "ch_cluster": "{cluster}",
        "ch_sharding_key": "rand()",
        "ch_only_shard": False,
    }

    assert adapter_defaults_module.build_load_target_create_kwargs(
        adapter,
        **common_kwargs,
        write_mode="append",
        original_target_exists=True,
    ) == {
        "gp_distributed_by_key": None,
        "partition_by": ["dt"],
        "order_by": ["id"],
    }
    assert adapter_defaults_module.build_create_from_sql_target_create_kwargs(
        adapter,
        **common_kwargs,
        drop_target_if_exists=True,
        target_exists_before_drop=True,
    ) == {
        "gp_distributed_by_key": None,
        "partition_by": ["dt"],
        "order_by": ["id"],
    }


def test_adapter_default_identifier_checks_empty_name() -> None:
    assert adapter_defaults_module._is_simple_identifier("") is False


def test_adapter_default_normalization_rejects_empty_values() -> None:
    adapter = get_backend_adapter("gp")

    assert (
        adapter_defaults_module.normalize_ch_columns_or_expression(
            adapter,
            " id ",
            "order_by",
        )
        == "id"
    )
    with pytest.raises(ValueError, match="must not be empty when provided"):
        adapter_defaults_module.normalize_ch_columns_or_expression(
            adapter,
            [],
            "order_by",
        )
    with pytest.raises(ValueError, match="order_by must not be empty"):
        adapter_defaults_module.normalize_ch_string(adapter, "  ", "order_by")


def test_adapter_default_partition_options_reject_backend_specific_inputs() -> None:
    with pytest.raises(
        InvalidSqlInputError,
        match="gp_truncate=True",
    ):
        adapter_defaults_module.validate_drop_partitions_options(
            object(),
            partition_column=None,
            gp_truncate=True,
        )
    with pytest.raises(
        InvalidSqlInputError,
        match="trino_partition_column",
    ):
        adapter_defaults_module.validate_drop_partitions_options(
            object(),
            partition_column="dt",
            gp_truncate=False,
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"ch_only_shard": True},
        {"ch_cluster": "analytics"},
        {"ch_sharding_key": "id"},
    ],
)
def test_adapter_default_rejects_clickhouse_only_create_options(
    overrides: dict[str, object],
) -> None:
    options: dict[str, Any] = {
        "option_owner": "to_db",
        "partition_by": None,
        "order_by": None,
        "ch_engine": "ReplicatedMergeTree",
        "ch_cluster": "{cluster}",
        "ch_sharding_key": "rand()",
        "ch_only_shard": False,
    }
    options.update(overrides)

    with pytest.raises(ValueError, match=r"can only be used.*type 'ch'"):
        adapter_defaults_module.validate_ch_create_table_options(
            get_backend_adapter("gp"),
            **options,
        )


def test_adapter_default_stage_discovery_and_qualification() -> None:
    class Cursor:
        def __init__(self) -> None:
            self.executed: list[tuple[str, tuple[str, str]]] = []
            self.closed = False

        def execute(self, sql: str, params: tuple[str, str]) -> None:
            self.executed.append((sql, params))

        def fetchall(self) -> list[tuple[str]]:
            return [("stage_one",), ("stage_two",)]

        def close(self) -> None:
            self.closed = True

    cursor = Cursor()
    connection = SimpleNamespace(cursor=lambda: cursor)
    adapter = get_backend_adapter("gp")

    assert adapter_defaults_module.query_transfer_stage_table_names(
        adapter,
        connection,
        connection_key="warehouse",
        transfer_staging_schema="stage-schema",
        table_pattern="target__stage__%",
    ) == ["stage_one", "stage_two"]
    assert cursor.executed[0][1] == ("stage-schema", "target__stage__%")
    assert cursor.closed is True
    assert (
        adapter_defaults_module.qualify_transfer_stage_table_name(
            adapter,
            "warehouse",
            "stage-schema",
            "1stage",
        )
        == '"stage-schema"."1stage"'
    )


def test_adapter_default_transfer_and_insert_policies() -> None:
    adapter = get_backend_adapter("gp")
    batch = pd.DataFrame({"id": [1]})
    error = RuntimeError("insert failed")

    assert (
        adapter_defaults_module.resolve_transfer_staging_mode(
            adapter,
            None,
            s3_transfer_staging_schema=None,
            s3_transfer_staging_location=None,
        )
        is None
    )
    with pytest.raises(ValueError, match="trino_mode must be one of"):
        adapter_defaults_module.resolve_transfer_staging_mode(
            adapter,
            "csv",
            s3_transfer_staging_schema=None,
            s3_transfer_staging_location=None,
        )
    assert adapter_defaults_module.normalize_insert_batch(adapter, batch) is batch
    assert adapter_defaults_module.normalize_insert_rows(adapter, [[1, "a"]]) == [(1, "a")]
    assert (
        adapter_defaults_module.should_wrap_insert_error_as_ambiguous(
            adapter,
            object(),
            error,
        )
        is True
    )
    assert adapter_defaults_module.should_refresh_connection_before_insert_retry(adapter) is False
    assert (
        adapter_defaults_module.wait_for_table_absence(
            adapter,
            object(),
            "target",
        )
        is None
    )
    assert (
        adapter_defaults_module.estimate_source_rows(
            adapter,
            object(),
            "SELECT 1",
        )
        is None
    )


def test_adapter_default_vacuum_restores_autocommit_after_failure() -> None:
    execute_error = RuntimeError("vacuum failed")

    class Cursor:
        def __init__(self) -> None:
            self.closed = False

        def execute(self, sql: str) -> None:
            assert sql == "VACUUM target"
            raise execute_error

        def close(self) -> None:
            self.closed = True

    cursor = Cursor()
    connection = SimpleNamespace(autocommit=False, cursor=lambda: cursor)
    adapter = SimpleNamespace(
        build_vacuum_table_sql=lambda table_name, **kwargs: f"VACUUM {table_name}"
    )

    with pytest.raises(RuntimeError, match="vacuum failed"):
        adapter_defaults_module.vacuum_table(adapter, connection, "target")

    assert connection.autocommit is False
    assert cursor.closed is True


def test_adapter_default_vacuum_supports_connection_without_autocommit() -> None:
    executed: list[str] = []
    cursor = SimpleNamespace(
        execute=executed.append,
        close=lambda: executed.append("closed"),
    )
    connection = SimpleNamespace(cursor=lambda: cursor)
    adapter = SimpleNamespace(
        build_vacuum_table_sql=lambda table_name, **kwargs: f"VACUUM {table_name}"
    )

    adapter_defaults_module.vacuum_table(adapter, connection, "target")

    assert executed == ["VACUUM target", "closed"]


def test_base_adapter_metadata_abstract_and_value_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = MinimalContractAdapter()
    assert adapter.name == "minimal"
    assert adapter.capability.display_name == "Minimal"

    executed: list[str] = []
    monkeypatch.setattr(
        adapter,
        "execute_command",
        lambda _connection, sql: executed.append(sql),
    )
    adapter.analyze_table(object(), "public.events", query_label="contract")
    assert executed
    assert "ANALYZE public.events" in executed[0]

    with pytest.raises(NotImplementedError):
        adapter.iter_source_batches(
            connection_key="source",
            connection_ref={"connection": object()},
            query="SELECT 1",
            get_batch_size=lambda: 1,
            retry_cnt=0,
            timeout_increment=0,
        )
    with pytest.raises(NotImplementedError):
        adapter.build_drop_upsert_partition_sqls(
            "target",
            partition_column="day",
            partition_values=None,
        )
    with pytest.raises(UnsupportedConnectionTypeError, match="does not support"):
        adapter.build_dataframe_batch_insert_sql("target", ["id"], row_count=1)

    with pytest.raises(ValueError, match="missing staged column"):
        adapter.column_types_for_columns({"id": "BIGINT"}, ["id", "value"])
    assert adapter.type_code_name(None, None, None) is None
    assert adapter.type_code_name(SimpleNamespace(type_name="custom"), None, None) == "custom"
    assert adapter.type_code_name(object(), None, None).startswith("<object object")
    with pytest.raises(ValueError, match="empty strings"):
        adapter.normalize_query_id("  ")
    assert adapter.normalize_query_id(7) == "7"
    with pytest.raises(ValueError, match="strings or integers"):
        adapter.normalize_query_id(True)
    assert "FROM (SELECT 1)" in adapter.build_insert_from_query_sql(
        "target", " SELECT 1; ", {"id": "BIGINT"}
    )


def test_base_adapter_write_stage_and_finalization_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = MinimalContractAdapter()
    monkeypatch.setattr(
        adapter,
        "build_clear_target_sqls",
        lambda *_args, **_kwargs: ["CLEAR target"],
    )
    assert adapter.transfer_replace_existing_non_ch() == "clear"
    assert adapter.build_transfer_replace_target_sqls("target") == ["CLEAR target"]
    assert adapter.transfer_replace_target_phase() == "clear_target"

    events: list[Any] = []
    monkeypatch.setattr(adapter, "clear_table", lambda *args, **kwargs: events.append("clear"))
    monkeypatch.setattr(adapter, "drop_table", lambda *args, **kwargs: events.append("drop"))

    def request(**values: Any) -> TargetWriteModeRequest:
        return TargetWriteModeRequest(
            connection=object(),
            table_name="target",
            write_mode=values.get("write_mode", "replace"),
            target_exists=values.get("target_exists", True),
            replace_existing_non_ch=values.get("policy", "clear"),
        )

    assert adapter.apply_target_write_mode(request(write_mode="append")) is True
    assert adapter.apply_target_write_mode(request(policy="drop")) is False
    with pytest.raises(ValueError, match="clear, drop"):
        adapter.apply_target_write_mode(request(policy="invalid"))
    assert events == ["drop"]

    creates: list[dict[str, Any]] = []
    monkeypatch.setattr(
        importlib.import_module("analytics_toolkit.sql.ddl.api"),
        "_create_sql_table_with_connection",
        lambda *args, **kwargs: creates.append(kwargs),
    )
    adapter.ensure_stage_target_table(
        StageTargetTableRequest(
            connection=object(),
            target_table="target",
            sample_batch=pd.DataFrame({"id": [1]}),
            target_column_types={"id": "BIGINT"},
            gp_distributed_by_key=None,
            partition_by=["day"],
            order_by=["id"],
            ch_engine="MergeTree",
            ch_cluster="cluster",
            ch_sharding_key="id",
            query_label=None,
            connection_key="minimal",
        )
    )
    assert creates[0]["partition_by"] == ["day"]
    assert creates[0]["order_by"] == ["id"]

    events.clear()
    monkeypatch.setattr(
        adapter,
        "ensure_stage_target_table",
        lambda _request: events.append("ensure") or True,
    )
    monkeypatch.setattr(
        adapter,
        "insert_from_table",
        lambda *args, **kwargs: events.append(("insert", args[2])),
    )
    for target_exists in (True, False):
        adapter.finalize_stage_table(
            StageFinalizationRequest(
                connection=object(),
                stage_table="stage",
                target_table="target",
                replace_target_table=False,
                target_exists=target_exists,
                sample_batch=pd.DataFrame({"id": [1]}),
            )
        )
    assert events == [("insert", "stage"), "ensure", ("insert", "stage")]


def test_default_execute_commands_dispatches_every_statement() -> None:
    calls: list[tuple[object, str]] = []
    connection = object()
    adapter = SimpleNamespace(
        execute_command=lambda current_connection, sql: calls.append((current_connection, sql))
    )

    backend_common_methods_module.execute_commands(
        adapter,
        connection,
        ["SELECT 1", "SELECT 2"],
    )

    assert calls == [(connection, "SELECT 1"), (connection, "SELECT 2")]
