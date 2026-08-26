from __future__ import annotations

from tests.sql._support.create_from_sql import (
    SOURCE_DESCRIPTION,
    FakeClickHouseClient,
    FakeDbapiConnection,
    SimpleNamespace,
    _candidate_create_options,
    ch_fast_path_module,
    create_module,
    importlib,
    pytest,
    sql_module,
)


def test_clickhouse_fast_path_applies_only_to_same_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = create_module.get_backend_adapter("ch")
    assert ch_fast_path_module.uses_create_table_from_sql_fast_path(
        adapter,
        source_backend="ch",
        source_key="ch",
        target_key="ch",
    )
    assert not ch_fast_path_module.uses_create_table_from_sql_fast_path(
        adapter,
        source_backend="gp",
        source_key="gp",
        target_key="ch",
    )
    assert ch_fast_path_module.create_table_from_sql_fast_path(
        adapter,
        source_backend="gp",
        source_key="gp",
        target_key="ch",
        target_table="events",
        source_sql="SELECT 1",
        partition_by=None,
        order_by=None,
        ch_engine="MergeTree",
        ch_cluster="cluster",
        ch_sharding_key="rand()",
        ch_only_shard=False,
        ch_retry_per_host_drops=True,
        insert_data=True,
        drop_target_if_exists=False,
        dry_run=False,
        return_sql=False,
        query_label=None,
        return_metadata=False,
        table_schema=None,
    ) == (False, None)

    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(
        importlib.import_module("analytics_toolkit.sql.backends.ch.create_table_as"),
        "ch_create_table_as",
        lambda *args, **kwargs: calls.append((args, kwargs)) or "created",
    )
    applied, result = ch_fast_path_module.create_table_from_sql_fast_path(
        adapter,
        source_backend="ch",
        source_key="ch",
        target_key="ch",
        target_table="events",
        source_sql="SELECT 1",
        partition_by=["dt"],
        order_by=["id"],
        ch_engine="MergeTree",
        ch_cluster="cluster",
        ch_sharding_key="id",
        ch_only_shard=True,
        ch_retry_per_host_drops=False,
        insert_data=False,
        drop_target_if_exists=True,
        dry_run=True,
        return_sql=False,
        query_label="job",
        return_metadata=True,
        table_schema={"id": "UInt64"},
    )
    assert (applied, result) == (True, "created")
    assert calls[0][0] == ("ch", "events", "SELECT 1")
    assert calls[0][1]["ch_only_shard"] is True


def test_create_from_sql_compatibility_delegation_and_fast_unsafe_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transfer_api = importlib.import_module("analytics_toolkit.sql.dml.transfer.flow.api")
    monkeypatch.setattr(transfer_api, "transfer_table", lambda **kwargs: kwargs["to_table"])
    assert create_module.transfer_table(to_table="sandbox.target") == "sandbox.target"

    adapter = SimpleNamespace(
        create_table_from_sql_fast_path=lambda **_kwargs: (False, None),
    )
    monkeypatch.setattr(create_module, "get_sql_connection", lambda _key: pytest.fail("no probe"))
    monkeypatch.setattr(create_module, "_cleanup_attempt_target", lambda **_kwargs: False)
    failure = create_module._execute_create_table_from_sql_fast_path_attempt(
        options=_candidate_create_options(drop_target_if_exists=True),
        target_adapter=adapter,
        attempt=2,
    )
    assert isinstance(failure, create_module._UnsafeAttemptFailure)
    assert failure.attempt == 2


def test_create_from_sql_generic_metadata_direct_and_delegated_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connections: list[FakeDbapiConnection] = []

    def open_connection(_key: str) -> FakeDbapiConnection:
        connection = FakeDbapiConnection()
        connections.append(connection)
        return connection

    adapter = SimpleNamespace(
        validate_ch_columns_in_columns=lambda *_a, **_k: None,
        prepare_existing_target_for_create_from_sql=lambda *_a, **_k: False,
        build_create_from_sql_target_create_kwargs=lambda **_k: {},
        should_insert_create_table_from_sql_directly=lambda **_k: True,
    )
    monkeypatch.setattr(create_module, "get_sql_connection", open_connection)
    monkeypatch.setattr(
        create_module,
        "inspect_source_query_schema",
        lambda *_a, **_k: [SimpleNamespace(name="id")],
    )
    monkeypatch.setattr(
        create_module,
        "map_source_schema_to_target",
        lambda *_a, **_k: {"id": "BIGINT"},
    )
    monkeypatch.setattr(create_module, "table_exists", lambda *_a, **_k: False)
    monkeypatch.setattr(create_module, "_create_sql_table_with_connection", lambda *_a, **_k: None)
    monkeypatch.setattr(create_module, "insert_from_query", lambda *_a, **_k: 3)

    no_insert = create_module._execute_generic_create_table_from_sql_attempt(
        options=_candidate_create_options(insert_data=False, return_metadata=True),
        target_adapter=adapter,
        attempt=1,
    )
    assert no_insert.rows is None

    direct = create_module._execute_generic_create_table_from_sql_attempt(
        options=_candidate_create_options(return_metadata=True),
        target_adapter=adapter,
        attempt=1,
    )
    assert direct.rows == 3
    assert direct.metadata.source_rows == 3

    adapter.should_insert_create_table_from_sql_directly = lambda **_k: False
    transfer_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        create_module,
        "transfer_table",
        lambda **kwargs: transfer_calls.append(kwargs) or 4,
    )
    delegated = create_module._execute_generic_create_table_from_sql_attempt(
        options=_candidate_create_options(
            source_key="gp_source",
            target_key="trino_target",
            target_backend="trino",
            table_schema={"id": "BIGINT"},
            query_label="candidate-9",
            return_metadata=True,
            ch_only_shard=True,
        ),
        target_adapter=adapter,
        attempt=1,
    )
    assert delegated == 4
    assert transfer_calls[0]["query_label"] == "candidate-9"
    assert transfer_calls[0]["table_schema"] == {"id": "BIGINT"}
    assert transfer_calls[0]["return_metadata"] is True
    assert all(connection.close_calls == 1 for connection in connections)


def test_create_table_from_sql_inserts_by_default(monkeypatch) -> None:
    connection = FakeDbapiConnection(
        description=SOURCE_DESCRIPTION,
        insert_rowcount=4,
    )
    monkeypatch.setattr(
        create_module,
        "get_sql_connection",
        lambda connection_key: connection,
    )

    inserted_rows = create_module.create_table_from_sql(
        "gp",
        "sandbox.target_table",
        "select id, amount from source_table",
    )

    assert inserted_rows == 4
    assert connection.executed[-1].startswith("INSERT INTO sandbox.target_table")


def test_create_table_from_sql_is_not_public() -> None:
    assert "create_table_from_sql" not in sql_module.__all__
    assert not hasattr(sql_module, "create_table_from_sql")


def test_create_table_from_sql_passes_only_shard_to_cross_backend_transfer(
    monkeypatch,
) -> None:
    source = FakeDbapiConnection(description=SOURCE_DESCRIPTION)
    target = FakeClickHouseClient()
    transfer_calls: list[dict[str, object]] = []

    def fake_get_sql_connection(connection_key: str) -> object:
        if connection_key == "gp":
            return source
        if connection_key == "ch":
            return target
        raise AssertionError(f"Unexpected connection key: {connection_key}")

    def fake_transfer_table(**kwargs: object) -> int:
        transfer_calls.append(kwargs)
        return 5

    monkeypatch.setattr(create_module, "get_sql_connection", fake_get_sql_connection)
    monkeypatch.setattr(create_module, "transfer_table", fake_transfer_table)

    inserted_rows = create_module.create_table_from_sql(
        "gp",
        "analytics.events",
        "select id, amount from source_table",
        table_db="ch",
        insert_data=True,
        ch_only_shard=True,
        order_by=["id"],
    )

    assert inserted_rows == 5
    assert transfer_calls[0]["ch_only_shard"] is True
    assert any(
        command.startswith("CREATE TABLE IF NOT EXISTS analytics.events\n")
        and "ENGINE = ReplicatedMergeTree" in command
        for command in target.commands
    )
    assert not any(
        command.startswith("CREATE TABLE IF NOT EXISTS analytics.events_shard")
        for command in target.commands
    )
    assert not any("ENGINE = Distributed(" in command for command in target.commands)


def test_create_table_from_sql_passes_table_schema_to_cross_backend_transfer(
    monkeypatch,
) -> None:
    source = FakeDbapiConnection(description=SOURCE_DESCRIPTION)
    target = FakeClickHouseClient()
    transfer_calls: list[dict[str, object]] = []

    def fake_get_sql_connection(connection_key: str) -> object:
        if connection_key == "gp":
            return source
        if connection_key == "ch":
            return target
        raise AssertionError(f"Unexpected connection key: {connection_key}")

    def fake_transfer_table(**kwargs: object) -> int:
        transfer_calls.append(kwargs)
        return 5

    monkeypatch.setattr(create_module, "get_sql_connection", fake_get_sql_connection)
    monkeypatch.setattr(create_module, "transfer_table", fake_transfer_table)

    inserted_rows = create_module.create_table_from_sql(
        "gp",
        "analytics.events",
        "select id, amount from source_table",
        table_db="ch",
        insert_data=True,
        table_schema={"id": "String", "amount": "Float64"},
    )

    assert inserted_rows == 5
    assert transfer_calls[0]["table_schema"] == {
        "id": "String",
        "amount": "Float64",
    }
    assert any(
        command.startswith("CREATE TABLE IF NOT EXISTS analytics.events_shard")
        and "`id` String" in command
        and "`amount` Float64" in command
        for command in target.commands
    )


def test_create_table_from_sql_same_clickhouse_delegates_to_adapter_fast_path(
    monkeypatch,
) -> None:
    calls: list[dict[str, object]] = []
    probe_connection = FakeClickHouseClient()

    def fake_fast_path(**kwargs: object) -> tuple[bool, object]:
        calls.append(kwargs)
        return True, "delegated"

    monkeypatch.setattr(
        create_module.get_backend_adapter("ch"),
        "create_table_from_sql_fast_path",
        fake_fast_path,
    )
    monkeypatch.setattr(
        create_module,
        "get_sql_connection",
        lambda connection_key: probe_connection,
    )

    result = create_module.create_table_from_sql(
        "ch",
        "analytics.events",
        "select id, amount from source_table;",
        order_by=["id"],
        insert_data=False,
        drop_target_if_exists=False,
        table_schema={"id": "UInt64", "amount": "Float64"},
    )

    assert result == "delegated"
    assert probe_connection.close_calls == 1
    assert calls == [
        {
            "source_backend": "ch",
            "source_key": "ch",
            "target_key": "ch",
            "target_table": "analytics.events",
            "source_sql": "select id, amount from source_table",
            "partition_by": None,
            "order_by": ["id"],
            "ch_engine": "ReplicatedMergeTree",
            "ch_cluster": "{cluster}",
            "ch_sharding_key": "rand()",
            "ch_only_shard": False,
            "ch_retry_per_host_drops": True,
            "insert_data": False,
            "drop_target_if_exists": False,
            "dry_run": False,
            "return_sql": False,
            "query_label": None,
            "return_metadata": False,
            "table_schema": {"id": "UInt64", "amount": "Float64"},
        }
    ]


def test_create_table_from_sql_table_schema_overrides_source_types(
    monkeypatch,
) -> None:
    connection = FakeDbapiConnection(
        description=SOURCE_DESCRIPTION,
        insert_rowcount=3,
    )
    monkeypatch.setattr(
        create_module,
        "get_sql_connection",
        lambda connection_key: connection,
    )

    inserted_rows = create_module.create_table_from_sql(
        "gp",
        "sandbox.target_table",
        "select id, amount from source_table",
        insert_data=True,
        table_schema={"id": "TEXT", "amount": "DOUBLE PRECISION"},
    )

    assert inserted_rows == 3
    assert any(
        sql.startswith("CREATE TABLE sandbox.target_table")
        and '"id" TEXT' in sql
        and '"amount" DOUBLE PRECISION' in sql
        for sql in connection.executed
    )
    assert connection.executed[-1] == (
        'INSERT INTO sandbox.target_table ("id", "amount") '
        'SELECT CAST("id" AS TEXT) AS "id", '
        'CAST("amount" AS DOUBLE PRECISION) AS "amount" '
        "FROM (select id, amount from source_table) AS source_query"
    )


def test_insert_data_same_backend_emits_typed_insert_and_returns_rowcount(
    monkeypatch,
) -> None:
    connection = FakeDbapiConnection(
        description=SOURCE_DESCRIPTION,
        insert_rowcount=7,
    )
    monkeypatch.setattr(
        create_module,
        "get_sql_connection",
        lambda connection_key: connection,
    )

    inserted_rows = create_module.create_table_from_sql(
        "gp",
        "sandbox.target_table",
        "select id, amount from source_table",
        insert_data=True,
    )

    assert inserted_rows == 7
    assert connection.executed[-1] == (
        'INSERT INTO sandbox.target_table ("id", "amount") '
        'SELECT CAST("id" AS INTEGER) AS "id", '
        'CAST("amount" AS NUMERIC(12, 2)) AS "amount" '
        "FROM (select id, amount from source_table) AS source_query"
    )


def test_insert_data_same_trino_preserves_array_types(monkeypatch) -> None:
    connection = FakeDbapiConnection(
        description=[
            ("campaign_codes", "array(varchar)", None, None, None, None),
            ("po_bonus_pk", "array(varbinary)", None, None, None, None),
            ("pers_offers_pk", "array(varbinary)", None, None, None, None),
        ],
        insert_rowcount=1,
    )
    monkeypatch.setattr(
        create_module,
        "get_sql_connection",
        lambda connection_key: connection,
    )

    inserted_rows = create_module.create_table_from_sql(
        "trino",
        "iceberg.sandbox.target_table",
        "select campaign_codes, po_bonus_pk, pers_offers_pk from source_table",
        insert_data=True,
    )

    assert inserted_rows == 1
    assert any(
        statement.startswith("CREATE TABLE iceberg.sandbox.target_table")
        and '"campaign_codes" array(varchar)' in statement
        and '"po_bonus_pk" array(varbinary)' in statement
        and '"pers_offers_pk" array(varbinary)' in statement
        for statement in connection.executed
    )
    assert connection.executed[-1] == (
        'INSERT INTO iceberg.sandbox.target_table ("campaign_codes", '
        '"po_bonus_pk", "pers_offers_pk") '
        'SELECT CAST("campaign_codes" AS array(varchar)) AS "campaign_codes", '
        'CAST("po_bonus_pk" AS array(varbinary)) AS "po_bonus_pk", '
        'CAST("pers_offers_pk" AS array(varbinary)) AS "pers_offers_pk" '
        "FROM (select campaign_codes, po_bonus_pk, pers_offers_pk from source_table) "
        "AS source_query"
    )
