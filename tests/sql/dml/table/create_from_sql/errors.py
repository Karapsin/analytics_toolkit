from __future__ import annotations

from tests.sql._support.create_from_sql import (
    SOURCE_DESCRIPTION,
    CloseFailDbapiConnection,
    FakeClickHouseClient,
    FakeDbapiConnection,
    SimpleNamespace,
    _candidate_create_options,
    create_module,
    pytest,
)


def test_create_from_sql_delegated_failure_retains_unsafe_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = SimpleNamespace(
        validate_ch_columns_in_columns=lambda *_a, **_k: None,
        prepare_existing_target_for_create_from_sql=lambda *_a, **_k: False,
        build_create_from_sql_target_create_kwargs=lambda **_k: {},
        should_insert_create_table_from_sql_directly=lambda **_k: False,
    )
    monkeypatch.setattr(create_module, "get_sql_connection", lambda _key: FakeDbapiConnection())
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
    monkeypatch.setattr(
        create_module,
        "transfer_table",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("transfer failed")),
    )
    monkeypatch.setattr(create_module, "_cleanup_attempt_target", lambda **_kwargs: False)
    result = create_module._execute_generic_create_table_from_sql_attempt(
        options=_candidate_create_options(),
        target_adapter=adapter,
        attempt=3,
    )
    assert isinstance(result, create_module._UnsafeAttemptFailure)
    assert result.attempt == 3


def test_create_table_from_sql_cleans_cross_backend_transfer_before_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_connections: list[FakeDbapiConnection] = []
    target_connections: list[FakeClickHouseClient] = []
    transfer_calls: list[dict[str, object]] = []
    target_exists = False
    cleanup_calls = 0

    def fake_get_sql_connection(connection_key: str) -> object:
        if connection_key == "gp":
            connection = FakeDbapiConnection(description=SOURCE_DESCRIPTION)
            source_connections.append(connection)
            return connection
        if connection_key == "ch":
            connection = FakeClickHouseClient()
            target_connections.append(connection)
            return connection
        raise AssertionError(f"Unexpected connection key: {connection_key}")

    def fake_create(*args: object, **kwargs: object) -> None:
        nonlocal target_exists
        target_exists = True

    def flaky_transfer(**kwargs: object) -> int:
        transfer_calls.append(kwargs)
        if len(transfer_calls) == 1:
            raise RuntimeError("temporary transfer failure")
        return 7

    def fake_drop(**kwargs: object) -> None:
        nonlocal cleanup_calls, target_exists
        cleanup_calls += 1
        target_exists = False

    monkeypatch.setattr(create_module, "get_sql_connection", fake_get_sql_connection)
    monkeypatch.setattr(
        create_module,
        "table_exists",
        lambda *args, **kwargs: target_exists,
    )
    monkeypatch.setattr(create_module, "_create_sql_table_with_connection", fake_create)
    monkeypatch.setattr(create_module, "transfer_table", flaky_transfer)
    monkeypatch.setattr(create_module, "_drop_attempt_target", fake_drop)

    result = create_module.create_table_from_sql(
        "gp",
        "analytics.events",
        "select id, amount from source_table",
        table_db="ch",
        retry_cnt=2,
        timeout_increment=0,
    )

    assert result == 7
    assert cleanup_calls == 1
    assert target_exists is True
    assert len(transfer_calls) == 2
    assert all(call["retry_cnt"] == 1 for call in transfer_calls)
    assert all(call["full_retry_cnt"] == 1 for call in transfer_calls)
    assert len(source_connections) == 2
    assert len(target_connections) == 3
    assert all(connection.close_calls == 1 for connection in source_connections)
    assert all(connection.close_calls == 1 for connection in target_connections)


def test_create_table_from_sql_cleans_direct_insert_before_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connections: list[FakeDbapiConnection] = []
    target_exists = False
    create_calls = 0
    insert_calls = 0
    cleanup_calls = 0

    def fake_get_sql_connection(connection_key: str) -> FakeDbapiConnection:
        assert connection_key == "gp"
        connection = FakeDbapiConnection(description=SOURCE_DESCRIPTION)
        connections.append(connection)
        return connection

    def fake_create(*args: object, **kwargs: object) -> None:
        nonlocal create_calls, target_exists
        create_calls += 1
        target_exists = True

    def flaky_insert(*args: object, **kwargs: object) -> int:
        nonlocal insert_calls
        insert_calls += 1
        if insert_calls == 1:
            raise RuntimeError("temporary insert failure")
        return 4

    def fake_drop(**kwargs: object) -> None:
        nonlocal cleanup_calls, target_exists
        cleanup_calls += 1
        target_exists = False

    monkeypatch.setattr(create_module, "get_sql_connection", fake_get_sql_connection)
    monkeypatch.setattr(
        create_module,
        "table_exists",
        lambda *args, **kwargs: target_exists,
    )
    monkeypatch.setattr(create_module, "_create_sql_table_with_connection", fake_create)
    monkeypatch.setattr(create_module, "insert_from_query", flaky_insert)
    monkeypatch.setattr(create_module, "_drop_attempt_target", fake_drop)

    result = create_module.create_table_from_sql(
        "gp",
        "sandbox.target_table",
        "select id, amount from source_table",
        retry_cnt=2,
        timeout_increment=0,
    )

    assert result == 4
    assert create_calls == 2
    assert insert_calls == 2
    assert cleanup_calls == 1
    assert target_exists is True
    assert len(connections) == 2
    assert [connection.close_calls for connection in connections] == [1, 1]


def test_create_table_from_sql_cleans_fast_path_before_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connections: list[FakeClickHouseClient] = []
    fast_path_calls = 0
    cleanup_calls = 0
    target_exists = False

    def fake_get_sql_connection(connection_key: str) -> FakeClickHouseClient:
        assert connection_key == "ch"
        connection = FakeClickHouseClient()
        connections.append(connection)
        return connection

    def flaky_fast_path(**kwargs: object) -> tuple[bool, object]:
        nonlocal fast_path_calls, target_exists
        fast_path_calls += 1
        target_exists = True
        if fast_path_calls == 1:
            raise RuntimeError("temporary ClickHouse insert failure")
        return True, "created"

    def fake_drop(**kwargs: object) -> None:
        nonlocal cleanup_calls, target_exists
        cleanup_calls += 1
        target_exists = False

    monkeypatch.setattr(create_module, "get_sql_connection", fake_get_sql_connection)
    monkeypatch.setattr(
        create_module,
        "table_exists",
        lambda *args, **kwargs: target_exists,
    )
    monkeypatch.setattr(
        create_module.get_backend_adapter("ch"),
        "create_table_from_sql_fast_path",
        flaky_fast_path,
    )
    monkeypatch.setattr(create_module, "_drop_attempt_target", fake_drop)

    result = create_module.create_table_from_sql(
        "ch",
        "analytics.events",
        "select id, amount from source_table",
        retry_cnt=2,
        timeout_increment=0,
    )

    assert result == "created"
    assert fast_path_calls == 2
    assert cleanup_calls == 1
    assert target_exists is True
    assert len(connections) == 3
    assert all(connection.close_calls == 1 for connection in connections)


def test_create_table_from_sql_cleans_type_mismatch_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TrinoTypeMismatchError(Exception):
        error_name = "TYPE_MISMATCH"

    connection = FakeDbapiConnection(
        description=[
            ("campaign_codes", "array(varchar)", None, None, None, None),
        ]
    )
    error = TrinoTypeMismatchError("Cannot cast array(varchar) to varchar")
    insert_calls = 0
    cleanup_calls = 0

    def fail_insert(*args: object, **kwargs: object) -> int:
        nonlocal insert_calls
        insert_calls += 1
        raise error

    def record_cleanup(**kwargs: object) -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1

    monkeypatch.setattr(
        create_module,
        "get_sql_connection",
        lambda connection_key: connection,
    )
    monkeypatch.setattr(create_module, "insert_from_query", fail_insert)
    monkeypatch.setattr(create_module, "_drop_attempt_target", record_cleanup)

    with pytest.raises(TrinoTypeMismatchError) as caught:
        create_module.create_table_from_sql(
            "trino",
            "iceberg.sandbox.target_table",
            "select campaign_codes from source_table",
            insert_data=True,
            retry_cnt=5,
            timeout_increment=0,
        )

    assert caught.value is error
    assert insert_calls == 1
    assert cleanup_calls == 1
    assert connection.close_calls == 1


def test_create_table_from_sql_close_failure_does_not_mask_success_or_leak_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = FakeDbapiConnection(description=SOURCE_DESCRIPTION)
    target = CloseFailDbapiConnection()

    def fake_get_sql_connection(connection_key: str) -> FakeDbapiConnection:
        return source if connection_key == "gp" else target

    monkeypatch.setattr(create_module, "get_sql_connection", fake_get_sql_connection)

    with pytest.warns(RuntimeWarning, match="Could not close SQL connection 'trino'"):
        result = create_module.create_table_from_sql(
            "gp",
            "sandbox.target_table",
            "select id, amount from source_table",
            table_db="trino",
            insert_data=False,
            retry_cnt=2,
            timeout_increment=0,
        )

    assert result is None
    assert target.close_calls == 1
    assert source.close_calls == 1


def test_create_table_from_sql_does_not_retry_duplicate_source_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connections: list[FakeDbapiConnection] = []
    duplicate_description = [
        ("suppliers_cheques", "int8", None, None, None, None),
        ("suppliers_cheques", "int8", None, None, None, None),
    ]

    def fake_get_sql_connection(connection_key: str) -> FakeDbapiConnection:
        assert connection_key == "gp"
        connection = FakeDbapiConnection(description=duplicate_description)
        connections.append(connection)
        return connection

    monkeypatch.setattr(create_module, "get_sql_connection", fake_get_sql_connection)

    with pytest.raises(
        ValueError,
        match="sql must not return duplicate columns: suppliers_cheques",
    ):
        create_module.create_table_from_sql(
            "gp",
            "sandbox.target_table",
            "select suppliers_cheques, suppliers_cheques from source_table",
            retry_cnt=5,
            timeout_increment=0,
        )

    assert len(connections) == 1
    assert connections[0].close_calls == 1
    assert not any(sql.startswith("CREATE TABLE") for sql in connections[0].executed)


def test_create_table_from_sql_does_not_retry_or_drop_preexisting_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeDbapiConnection(description=SOURCE_DESCRIPTION)
    create_calls = 0

    def fail_create(*args: object, **kwargs: object) -> None:
        nonlocal create_calls
        create_calls += 1
        raise RuntimeError("target already exists")

    monkeypatch.setattr(
        create_module,
        "get_sql_connection",
        lambda connection_key: connection,
    )
    monkeypatch.setattr(
        create_module,
        "table_exists",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(create_module, "_create_sql_table_with_connection", fail_create)
    monkeypatch.setattr(
        create_module,
        "_drop_attempt_target",
        lambda **kwargs: pytest.fail("pre-existing target must not be dropped"),
    )

    with pytest.raises(RuntimeError, match="target already exists"):
        create_module.create_table_from_sql(
            "gp",
            "sandbox.target_table",
            "select id, amount from source_table",
            retry_cnt=3,
            timeout_increment=0,
        )

    assert create_calls == 1
    assert connection.close_calls == 1


def test_create_table_from_sql_retries_schema_inspection_with_fresh_connections(
    monkeypatch,
) -> None:
    connections: list[FakeDbapiConnection] = []
    inspection_calls = 0
    inspect_source_query_schema = create_module.inspect_source_query_schema

    def fake_get_sql_connection(connection_key: str) -> FakeDbapiConnection:
        assert connection_key == "gp"
        connection = FakeDbapiConnection(description=SOURCE_DESCRIPTION)
        connections.append(connection)
        return connection

    def flaky_inspect_source_query_schema(*args: object, **kwargs: object) -> object:
        nonlocal inspection_calls
        inspection_calls += 1
        if inspection_calls == 1:
            raise RuntimeError("temporary schema inspection failure")
        return inspect_source_query_schema(*args, **kwargs)

    monkeypatch.setattr(create_module, "get_sql_connection", fake_get_sql_connection)
    monkeypatch.setattr(
        create_module,
        "inspect_source_query_schema",
        flaky_inspect_source_query_schema,
    )

    result = create_module.create_table_from_sql(
        "gp",
        "sandbox.target_table",
        "select id, amount from source_table",
        insert_data=False,
        retry_cnt=2,
        timeout_increment=0,
    )

    assert result is None
    assert inspection_calls == 2
    assert len(connections) == 2
    assert [connection.close_calls for connection in connections] == [1, 1]
    assert not any(sql.startswith("CREATE TABLE") for sql in connections[0].executed)
    assert any(
        sql.startswith("CREATE TABLE sandbox.target_table") for sql in connections[1].executed
    )


def test_create_table_from_sql_stops_retry_when_partial_target_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connections: list[FakeDbapiConnection] = []
    create_calls = 0
    insert_calls = 0
    cleanup_calls = 0

    def fake_get_sql_connection(connection_key: str) -> FakeDbapiConnection:
        assert connection_key == "gp"
        connection = FakeDbapiConnection(description=SOURCE_DESCRIPTION)
        connections.append(connection)
        return connection

    def fake_create(*args: object, **kwargs: object) -> None:
        nonlocal create_calls
        create_calls += 1

    def fail_insert(*args: object, **kwargs: object) -> int:
        nonlocal insert_calls
        insert_calls += 1
        raise RuntimeError("original insert failure")

    def fail_cleanup(**kwargs: object) -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        raise RuntimeError("cleanup failure")

    monkeypatch.setattr(create_module, "get_sql_connection", fake_get_sql_connection)
    monkeypatch.setattr(create_module, "table_exists", lambda *args, **kwargs: False)
    monkeypatch.setattr(create_module, "_create_sql_table_with_connection", fake_create)
    monkeypatch.setattr(create_module, "insert_from_query", fail_insert)
    monkeypatch.setattr(create_module, "_drop_attempt_target", fail_cleanup)

    with pytest.warns(RuntimeWarning, match="Could not remove partial target"):
        with pytest.raises(RuntimeError, match="original insert failure"):
            create_module.create_table_from_sql(
                "gp",
                "sandbox.target_table",
                "select id, amount from source_table",
                retry_cnt=3,
                timeout_increment=0,
            )

    assert create_calls == 1
    assert insert_calls == 1
    assert cleanup_calls == 2
    assert len(connections) == 2
    assert [connection.close_calls for connection in connections] == [1, 1]


def test_create_table_from_sql_stops_retry_when_partial_target_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connections: list[FakeDbapiConnection] = []
    create_calls = 0
    insert_calls = 0
    cleanup_calls = 0

    def fake_get_sql_connection(connection_key: str) -> FakeDbapiConnection:
        assert connection_key == "gp"
        connection = FakeDbapiConnection(description=SOURCE_DESCRIPTION)
        connections.append(connection)
        return connection

    def fake_create(*args: object, **kwargs: object) -> None:
        nonlocal create_calls
        create_calls += 1

    def fail_insert(*args: object, **kwargs: object) -> int:
        nonlocal insert_calls
        insert_calls += 1
        raise RuntimeError("temporary insert failure")

    def fail_cleanup(**kwargs: object) -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        raise RuntimeError("cleanup unavailable")

    monkeypatch.setattr(create_module, "get_sql_connection", fake_get_sql_connection)
    monkeypatch.setattr(create_module, "table_exists", lambda *args, **kwargs: False)
    monkeypatch.setattr(create_module, "_create_sql_table_with_connection", fake_create)
    monkeypatch.setattr(create_module, "insert_from_query", fail_insert)
    monkeypatch.setattr(create_module, "_drop_attempt_target", fail_cleanup)

    with pytest.warns(RuntimeWarning, match="Could not remove partial target"):
        with pytest.raises(RuntimeError, match="temporary insert failure"):
            create_module.create_table_from_sql(
                "gp",
                "sandbox.target_table",
                "select id, amount from source_table",
                retry_cnt=3,
                timeout_increment=0,
            )

    assert create_calls == 1
    assert insert_calls == 1
    assert cleanup_calls == 2
    assert len(connections) == 2
    assert [connection.close_calls for connection in connections] == [1, 1]
