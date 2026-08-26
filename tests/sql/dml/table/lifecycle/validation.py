from __future__ import annotations

from tests.sql._support.lifecycle import (
    Any,
    SimpleNamespace,
    SqlOperationContext,
    UnsupportedConnectionTypeError,
    ValidationAdapter,
    backend_upsert,
    backend_validation,
    errors,
    maintenance,
    pytest,
    table_validation,
)


def test_backend_validation_single_stage_delegates_to_adapter() -> None:
    calls: list[tuple[str, Any]] = []
    adapter = SimpleNamespace(
        build_stage_duplicate_keys_sql=lambda table, keys: (
            calls.append((table, keys)) or "SELECT duplicate"
        )
    )
    assert (
        backend_validation.build_stage_duplicate_keys_sql_for_tables(
            adapter, ["only_stage"], ["id"]
        )
        == "SELECT duplicate"
    )
    assert calls == [("only_stage", ["id"])]


def test_gp_vacuum_rejects_non_gp_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        maintenance,
        "get_connection_config",
        lambda _key: SimpleNamespace(backend="trino", connection_key="warehouse"),
    )

    with pytest.raises(UnsupportedConnectionTypeError, match="requires a gp connection"):
        maintenance.gp_vacuum("warehouse", "schema.table")


def test_gp_vacuum_requires_db_key() -> None:
    with pytest.raises(TypeError, match="db_key"):
        maintenance.gp_vacuum(table_name="schema.table")


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (1, "string or sequence"),
        (["id", 1], "only string"),
        ([], "must not be empty"),
        ([" "], "empty column"),
        (["id", "id"], "duplicate"),
    ],
)
def test_normalize_key_columns_rejects_invalid_values(value: Any, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        table_validation.normalize_key_columns(value, "keys")


@pytest.mark.parametrize(
    ("value", "message"),
    [(1, "must be a string"), (" ", "must not be empty"), ("date + 1", "not a SQL expression")],
)
def test_normalize_upsert_partition_column_rejects_invalid_values(
    value: Any,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        table_validation.normalize_upsert_partition_column(value)


def test_operation_error_and_context_note_omit_missing_optional_fields() -> None:
    context = SqlOperationContext(operation="read")

    wrapped = errors.operation_error(ValueError("bad"), context)

    assert str(wrapped) == "SQL operation failed (read): ValueError: bad"
    assert errors._format_context_note(context) == "SQL context: operation=read"


def test_shared_upsert_source_requires_columns_and_builds_union() -> None:
    adapter = SimpleNamespace(quote_identifier=lambda column: f'"{column}"')
    assert backend_upsert.incoming_stage_source_sql(adapter, "stage") == "stage"
    with pytest.raises(ValueError, match="columns are required"):
        backend_upsert.incoming_stage_source_sql(
            adapter,
            "stage",
            incoming_stage_tables=["stage_a", "stage_b"],
        )
    assert backend_upsert.incoming_stage_source_sql(
        adapter,
        "stage",
        incoming_stage_tables=["stage_a", "stage_b"],
        columns=["id", "value"],
    ) == ('(\nSELECT "id", "value" FROM stage_a\nUNION ALL\nSELECT "id", "value" FROM stage_b\n)')


def test_table_validation_adapter_helpers_cover_multi_and_single_stage_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = ValidationAdapter()
    connection = object()
    monkeypatch.setattr(table_validation, "get_backend_adapter", lambda _backend: adapter)

    assert (
        table_validation._stage_has_duplicate_keys(
            "gp", connection, "stage", ["id"], stage_tables=["a", "b"]
        )
        is True
    )
    assert table_validation._stage_has_duplicate_keys("gp", connection, "stage", ["id"]) is False
    assert (
        table_validation._stage_keys_overlap_target("gp", connection, "stage", "target", ["id"])
        is True
    )
    assert table_validation._null_safe_key_equality("gp", "left", "right", "id") == (
        "left.id IS NOT DISTINCT FROM right.id"
    )
    assert [call[0] for call in adapter.calls] == [
        "build",
        "query",
        "duplicate",
        "overlap",
        "equality",
    ]


def test_validate_key_columns_in_columns_handles_skip_success_and_missing() -> None:
    table_validation.validate_key_columns_in_columns(None, ["id"])
    table_validation.validate_key_columns_in_columns(["id"], ["id", "value"])

    with pytest.raises(ValueError, match="missing"):
        table_validation.validate_key_columns_in_columns(["id", "missing"], ["id"])


def test_validate_stage_target_key_overlap_reports_conflicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(table_validation, "time_print", lambda *_args: None)
    monkeypatch.setattr(
        table_validation,
        "_stage_keys_overlap_target",
        lambda **_kwargs: False,
    )
    table_validation.validate_stage_target_key_overlap(
        "gp", object(), "stage", "target", ["id"], True, False
    )

    monkeypatch.setattr(
        table_validation,
        "_stage_keys_overlap_target",
        lambda **_kwargs: True,
    )
    with pytest.raises(ValueError, match="already exist"):
        table_validation.validate_stage_target_key_overlap(
            "gp", object(), "stage", "target", ["id"], True, False
        )


@pytest.mark.parametrize(
    ("key_columns", "target_exists", "replace_target"),
    [(None, True, False), (["id"], False, False), (["id"], True, True)],
)
def test_validate_stage_target_key_overlap_skips_inapplicable_checks(
    monkeypatch: pytest.MonkeyPatch,
    key_columns: list[str] | None,
    target_exists: bool,
    replace_target: bool,
) -> None:
    monkeypatch.setattr(
        table_validation,
        "_stage_keys_overlap_target",
        lambda **_kwargs: pytest.fail("overlap query must not run"),
    )

    table_validation.validate_stage_target_key_overlap(
        "gp",
        object(),
        "stage",
        "target",
        key_columns,
        target_exists,
        replace_target,
    )


def test_validate_stage_uniqueness_handles_skip_success_and_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logs: list[str] = []
    monkeypatch.setattr(table_validation, "time_print", logs.append)
    monkeypatch.setattr(
        table_validation,
        "_stage_has_duplicate_keys",
        lambda *_args, **_kwargs: False,
    )

    table_validation.validate_stage_uniqueness("gp", object(), "stage", None)
    table_validation.validate_stage_uniqueness(
        "gp",
        object(),
        "stage",
        ["id"],
        stage_tables=["stage_1", "stage_2"],
    )
    assert "stage_1, stage_2" in logs[0]

    monkeypatch.setattr(
        table_validation,
        "_stage_has_duplicate_keys",
        lambda *_args, **_kwargs: True,
    )
    with pytest.raises(ValueError, match="Duplicate key"):
        table_validation.validate_stage_uniqueness("gp", object(), "stage", ["id"])


def test_validate_upsert_partition_column_in_columns() -> None:
    table_validation.validate_upsert_partition_column_in_columns(None, [])
    table_validation.validate_upsert_partition_column_in_columns("date", ["date"])

    with pytest.raises(ValueError, match="missing"):
        table_validation.validate_upsert_partition_column_in_columns("missing", ["date"])
