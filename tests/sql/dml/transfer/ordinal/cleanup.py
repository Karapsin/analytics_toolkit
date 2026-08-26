from __future__ import annotations

from tests.sql._support.transfer_ordinal import (
    Any,
    SimpleNamespace,
    TransferOptions,
    TransferStageState,
    _staged_options,
    cleanup_superseded_transfer_stages,
    pytest,
    resolve_internal_columns,
    staged_attempt,
    superseded,
)


def test_superseded_cleanup_preserves_unverifiable_and_current_stages(monkeypatch: Any) -> None:
    internal = resolve_internal_columns(["id"], "gp")
    options = _staged_options()
    assert (
        cleanup_superseded_transfer_stages(
            options=options,
            connection=object(),
            backend="gp",
            connection_key="target",
            staging_schema=None,
            internal_columns=internal,
        )
        == []
    )

    adapter = SimpleNamespace(
        query_transfer_stage_table_names=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("discovery failed")
        ),
    )
    monkeypatch.setattr(
        "analytics_toolkit.sql.dml.transfer.flow.superseded.get_backend_adapter",
        lambda _backend: adapter,
    )
    assert (
        cleanup_superseded_transfer_stages(
            options=options,
            connection=object(),
            backend="gp",
            connection_key="target",
            staging_schema="stage",
            internal_columns=internal,
        )
        == []
    )

    candidate = f"{options.destination_hash}__target__stage__{'b' * 32}"
    adapter = SimpleNamespace(
        query_transfer_stage_table_names=lambda *_args, **_kwargs: [
            "wrong_prefix__" + "b" * 32,
            f"{options.destination_hash}__legacy",
            candidate,
        ],
        qualify_transfer_stage_table_name=lambda _key, schema, table: f"{schema}.{table}",
        quote_identifier=lambda value: f'"{value}"',
    )
    monkeypatch.setattr(
        "analytics_toolkit.sql.dml.transfer.flow.superseded.get_backend_adapter",
        lambda _backend: adapter,
    )
    assert (
        cleanup_superseded_transfer_stages(
            options=options,
            connection=object(),
            backend="gp",
            connection_key="target",
            staging_schema="stage",
            internal_columns=internal,
        )
        == []
    )

    current_empty = f"{options.destination_hash}__target__stage__{options.transfer_id}__s00000"
    adapter.query_transfer_stage_table_names = lambda *_args, **_kwargs: [current_empty]
    dropped: list[str] = []
    monkeypatch.setattr(
        superseded,
        "cleanup_stage_table",
        lambda _backend, _connection, table, **_kwargs: dropped.append(table),
    )
    assert cleanup_superseded_transfer_stages(
        options=options,
        connection=object(),
        backend="gp",
        connection_key="source",
        staging_schema="stage",
        internal_columns=internal,
        include_current_transfer_id=True,
    ) == [f"stage.{current_empty}"]
    assert dropped == [f"stage.{current_empty}"]

    stale_empty = f"{options.destination_hash}__target__stage__{'b' * 32}__s00000"
    adapter.query_transfer_stage_table_names = lambda *_args, **_kwargs: [stale_empty]
    dropped.clear()
    assert cleanup_superseded_transfer_stages(
        options=options,
        connection=object(),
        backend="gp",
        connection_key="target",
        staging_schema="stage",
        internal_columns=internal,
    ) == [f"stage.{stale_empty}"]
    assert dropped == [f"stage.{stale_empty}"]

    adapter.query_transfer_stage_table_names = lambda *_args, **_kwargs: [current_empty]
    assert (
        cleanup_superseded_transfer_stages(
            options=options,
            connection=object(),
            backend="gp",
            connection_key="target",
            staging_schema="stage",
            internal_columns=internal,
        )
        == []
    )


def test_superseded_cleanup_uses_reserved_stage_names(monkeypatch: Any) -> None:
    dropped: list[str] = []
    adapter = SimpleNamespace(
        query_transfer_stage_table_names=lambda *_args, **_kwargs: [
            "0123456789abcdef__orders" + "a" * 32 + "__w00000",
            "0123456789abcdef__orders" + "b" * 32 + "__source",
            "0123456789abcdef__orders" + "d" * 32 + "__other",
        ],
        qualify_transfer_stage_table_name=lambda _key, schema, table: f"{schema}.{table}",
    )
    monkeypatch.setattr(
        "analytics_toolkit.sql.dml.transfer.flow.superseded.get_backend_adapter",
        lambda _backend: adapter,
    )
    monkeypatch.setattr(
        "analytics_toolkit.sql.dml.transfer.flow.superseded.cleanup_stage_table",
        lambda _backend, _connection, table, **_kwargs: dropped.append(table),
    )
    options = TransferOptions(
        from_db_key="source",
        from_db_backend="gp",
        to_db_key="target",
        to_db_backend="gp",
        source_sql="SELECT 1",
        target_table="sales.orders",
        transfer_id="c" * 32,
        canonical_destination_identity="sales.orders",
        destination_hash="0123456789abcdef",
    )

    cleanup_superseded_transfer_stages(
        options=options,
        connection=object(),
        backend="gp",
        connection_key="target",
        staging_schema="staging",
        internal_columns=resolve_internal_columns(["id"], "gp"),
    )

    assert dropped == [
        "staging.0123456789abcdef__orders" + "a" * 32 + "__w00000",
        "staging.0123456789abcdef__orders" + "b" * 32 + "__source",
    ]


def test_unkeyed_base_exception_cleanup_does_not_mask_original(monkeypatch: Any) -> None:
    connections: list[Any] = []
    cleanup_calls: list[bool] = []

    class Connection:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    def open_connection(_key: str) -> Connection:
        connection = Connection()
        connections.append(connection)
        return connection

    monkeypatch.setattr(staged_attempt, "get_sql_connection", open_connection)
    monkeypatch.setattr(
        staged_attempt,
        "create_stage_state",
        lambda *_args: TransferStageState(target_exists=True),
    )
    monkeypatch.setattr(
        staged_attempt,
        "inspect_source_query_schema",
        lambda *_args: (_ for _ in ()).throw(KeyboardInterrupt("cancelled")),
    )

    def fail_cleanup(*_args: Any, **kwargs: Any) -> None:
        cleanup_calls.append(kwargs["drop_created_target"])
        raise RuntimeError("cleanup must not mask cancellation")

    monkeypatch.setattr(staged_attempt, "cleanup_stage", fail_cleanup)

    with pytest.raises(KeyboardInterrupt, match="cancelled"):
        staged_attempt.run_staged_source_transfer_attempt(
            _staged_options(),
            insert_retry_cnt=1,
        )

    assert cleanup_calls == [True]
    assert all(connection.closed for connection in connections)
