from __future__ import annotations

from tests.sql._support.create_from_sql import (
    FakeDbapiConnection,
    SimpleNamespace,
    _candidate_create_options,
    create_module,
    pytest,
)


def test_create_from_sql_cleanup_validation_bool_and_close_dedup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="at least one column"):
        create_module._validate_source_columns([])
    with pytest.raises(ValueError, match="duplicate columns"):
        create_module._validate_source_columns(["id", "id", "value"])
    with pytest.raises(ValueError, match="boolean"):
        create_module._normalize_only_shard(1)

    options = _candidate_create_options(query_label="candidate-9")
    drop_calls: list[dict[str, object]] = []
    adapter = SimpleNamespace(
        rollback_quietly=lambda _connection: None,
        prepare_existing_target_for_create_from_sql=lambda *_a, **kwargs: drop_calls.append(kwargs),
    )
    create_module._drop_attempt_target(
        options=options,
        target_adapter=adapter,
        target_connection=object(),
    )
    assert drop_calls[0]["drop_target_if_exists"] is True

    monkeypatch.setattr(
        create_module,
        "get_sql_connection",
        lambda _key: (_ for _ in ()).throw(RuntimeError("cleanup unavailable")),
    )
    with pytest.warns(RuntimeWarning, match="Could not remove partial target"):
        assert not create_module._cleanup_attempt_target(
            options=options,
            target_adapter=adapter,
            target_connection=None,
        )

    connection = FakeDbapiConnection()
    create_module._close_connections(
        source_connection=connection,
        source_key="gp",
        source_backend="gp",
        target_connection=connection,
        target_key="gp",
        target_backend="gp",
    )
    assert connection.close_calls == 1

    create_module._close_connections(
        source_connection=None,
        source_key="gp",
        source_backend="gp",
        target_connection=None,
        target_key="gp",
        target_backend="gp",
    )


def test_create_table_from_sql_validates_empty_inputs(monkeypatch) -> None:
    monkeypatch.setattr(
        create_module,
        "get_sql_connection",
        lambda connection_key: pytest.fail("connection should not be opened"),
    )

    with pytest.raises(create_module.InvalidSqlInputError, match="table_name"):
        create_module.create_table_from_sql("gp", " ", "select 1")

    with pytest.raises(create_module.InvalidSqlInputError, match="sql"):
        create_module.create_table_from_sql("gp", "target", " ")

    with pytest.raises(create_module.InvalidSqlInputError, match="exactly one"):
        create_module.create_table_from_sql("gp", "target", "select 1; select 2")
