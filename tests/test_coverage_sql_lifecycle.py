from __future__ import annotations

import importlib
from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from analytics_toolkit.sql.connection.errors import (
    SqlOperationContext,
    SqlOperationError,
    UnsupportedConnectionTypeError,
)

maintenance = importlib.import_module("analytics_toolkit.sql.dml.table.maintenance")
table_validation = importlib.import_module("analytics_toolkit.sql.dml.table.table_validation")
errors = importlib.import_module("analytics_toolkit.sql.connection.errors")
backend_utils = importlib.import_module("analytics_toolkit.sql.backends.utils")
ch_options = importlib.import_module("analytics_toolkit.sql.clickhouse.options")
backends = importlib.import_module("analytics_toolkit.sql.backends")


class LifecycleAdapter:
    def __init__(self, *, analyze: bool = True) -> None:
        self.analyze = analyze
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))

    def should_analyze_table(self) -> bool:
        return self.analyze

    def analyze_table_sql(self, table_name: str, *, query_label: str | None) -> str:
        self._record("analyze_table_sql", table_name, query_label=query_label)
        return f"ANALYZE {table_name}"

    def analyze_table(
        self,
        connection: Any,
        table_name: str,
        *,
        query_label: str | None,
    ) -> None:
        self._record("analyze_table", connection, table_name, query_label=query_label)

    def vacuum_table(self, connection: Any, table_name: str, **kwargs: Any) -> None:
        self._record("vacuum_table", connection, table_name, **kwargs)

    def rollback_quietly(self, connection: Any) -> None:
        self._record("rollback_quietly", connection)

    def drop_table(self, connection: Any, table_name: str, **kwargs: Any) -> None:
        self._record("drop_table", connection, table_name, **kwargs)

    def wait_for_table_absence(
        self,
        connection: Any,
        table_name: str,
        **kwargs: Any,
    ) -> None:
        self._record("wait_for_table_absence", connection, table_name, **kwargs)

    def drop_table_with_options(self, connection: Any, table_name: str, **kwargs: Any) -> None:
        self._record("drop_table_with_options", connection, table_name, **kwargs)

    def build_clear_target_sqls(self, table_name: str, **kwargs: Any) -> list[str]:
        self._record("build_clear_target_sqls", table_name, **kwargs)
        return ["TRUNCATE shard", "TRUNCATE distributed"]

    def execute_commands(self, connection: Any, sqls: list[str]) -> None:
        self._record("execute_commands", connection, sqls)


@pytest.fixture
def lifecycle_adapter(monkeypatch: pytest.MonkeyPatch) -> LifecycleAdapter:
    adapter = LifecycleAdapter()
    monkeypatch.setattr(maintenance, "resolve_connection_backend", lambda value: value)
    monkeypatch.setattr(maintenance, "get_backend_adapter", lambda _backend: adapter)
    monkeypatch.setattr(maintenance, "time_print", lambda *_args, **_kwargs: None)
    return adapter


def test_analyze_table_returns_skipped_plan_for_noop_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = LifecycleAdapter(analyze=False)
    monkeypatch.setattr(maintenance, "resolve_connection_backend", lambda _value: "ch")
    monkeypatch.setattr(maintenance, "get_backend_adapter", lambda _backend: adapter)

    assert maintenance.analyze_table("alias", object(), "db.table") is None
    plan = maintenance.analyze_table(
        "alias",
        object(),
        "db.table",
        query_label="q",
        dry_run=True,
    )

    assert plan.options == {"skipped": True, "reason": "ch analyze is a no-op"}
    assert plan.metadata.statement_count == 0
    assert plan.sqls == []


def test_analyze_table_builds_plan_or_calls_adapter(
    lifecycle_adapter: LifecycleAdapter,
) -> None:
    connection = object()

    plan = maintenance.analyze_table(
        "gp",
        connection,
        "schema.table",
        query_label="q",
        return_sql=True,
    )
    result = maintenance.analyze_table("gp", connection, "schema.table")

    assert plan.sqls == ["ANALYZE schema.table"]
    assert plan.statements[0].phase == "analyze"
    assert plan.metadata.statement_count == 1
    assert result is None
    assert lifecycle_adapter.calls[-1][0] == "analyze_table"


def test_gp_vacuum_rejects_non_gp_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        maintenance,
        "get_connection_config",
        lambda _key: SimpleNamespace(backend="trino", connection_key="warehouse"),
    )

    with pytest.raises(UnsupportedConnectionTypeError, match="requires a gp connection"):
        maintenance.gp_vacuum("schema.table", db_key="warehouse")


@pytest.mark.parametrize("vacuum_error", [None, RuntimeError("vacuum failed")])
def test_gp_vacuum_always_closes_connection(
    monkeypatch: pytest.MonkeyPatch,
    vacuum_error: Exception | None,
) -> None:
    closed: list[bool] = []
    connection = SimpleNamespace(close=lambda: closed.append(True))
    adapter = LifecycleAdapter()

    def vacuum_table(*args: Any, **kwargs: Any) -> None:
        adapter._record("vacuum_table", *args, **kwargs)
        if vacuum_error is not None:
            raise vacuum_error

    adapter.vacuum_table = vacuum_table
    monkeypatch.setattr(
        maintenance,
        "get_connection_config",
        lambda _key: SimpleNamespace(backend="gp", connection_key="gp_alias"),
    )
    monkeypatch.setattr(maintenance, "get_sql_connection", lambda _key: connection)
    monkeypatch.setattr(maintenance, "get_backend_adapter", lambda _backend: adapter)
    monkeypatch.setattr(maintenance, "time_print", lambda *_args, **_kwargs: None)

    if vacuum_error is None:
        maintenance.gp_vacuum(
            "schema.table",
            analyze=True,
            full=True,
            verbose=False,
            db_key="gp_alias",
        )
    else:
        with pytest.raises(RuntimeError, match="vacuum failed"):
            maintenance.gp_vacuum("schema.table", db_key="gp_alias")

    assert closed == [True]
    assert adapter.calls[0][0] == "vacuum_table"


def test_drop_table_with_retry_runs_successful_operation(
    monkeypatch: pytest.MonkeyPatch,
    lifecycle_adapter: LifecycleAdapter,
) -> None:
    retry_kwargs: dict[str, Any] = {}
    drops: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        maintenance,
        "drop_table",
        lambda *args, **kwargs: drops.append((*args, kwargs)),
    )

    def retry_fn(**kwargs: Any) -> None:
        retry_kwargs.update(kwargs)
        kwargs["operation"](1)

    maintenance.drop_table_with_retry(
        "gp",
        "alias",
        {"connection": "connection"},
        "schema.stage",
        retry_fn,
        retry_cnt=2,
        timeout_increment=0.5,
        rollback_fn=None,
        replace_connection_fn=lambda *_args: None,
        query_label="q",
        if_exists=False,
    )

    assert drops[0][:3] == ("gp", "connection", "schema.stage")
    assert retry_kwargs["retry_cnt"] == 2
    assert retry_kwargs["timeout_increment"] == 0.5
    assert "dropping stage table" in retry_kwargs["operation_name"]
    assert lifecycle_adapter.calls == []


def test_drop_table_with_retry_rolls_back_replaces_and_reraises(
    monkeypatch: pytest.MonkeyPatch,
    lifecycle_adapter: LifecycleAdapter,
) -> None:
    connection_ref = {"connection": object()}
    replacements: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        maintenance,
        "drop_table",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("drop failed")),
    )

    def retry_fn(**kwargs: Any) -> None:
        kwargs["operation"](1)

    with pytest.raises(RuntimeError, match="drop failed"):
        maintenance.drop_table_with_retry(
            "gp",
            "alias",
            connection_ref,
            "schema.stage",
            retry_fn,
            retry_cnt=0,
            timeout_increment=0,
            rollback_fn=None,
            replace_connection_fn=lambda key, ref: replacements.append((key, ref)),
        )

    assert lifecycle_adapter.calls[0] == (
        "rollback_quietly",
        (connection_ref["connection"],),
        {},
    )
    assert replacements == [("alias", connection_ref)]


def test_drop_table_returns_plan_or_executes_and_waits(
    monkeypatch: pytest.MonkeyPatch,
    lifecycle_adapter: LifecycleAdapter,
) -> None:
    monkeypatch.setattr(
        maintenance,
        "build_drop_table_sql",
        lambda *_args, **_kwargs: "DROP TABLE schema.target",
    )
    connection = object()

    plan = maintenance.drop_table(
        "gp",
        connection,
        "schema.target",
        query_label="q",
        dry_run=True,
    )
    result = maintenance.drop_table(
        "gp",
        connection,
        "schema.target",
        ch_cluster="cluster",
        if_exists=False,
        wait_for_absence=True,
    )

    assert plan.sqls == ["DROP TABLE schema.target"]
    assert plan.statements[0].phase == "drop_target"
    assert result is None
    assert [call[0] for call in lifecycle_adapter.calls] == [
        "drop_table",
        "wait_for_table_absence",
    ]


@pytest.mark.parametrize(
    ("connection_key", "expected_retry"),
    [(None, False), ("ch_alias", True)],
)
def test_drop_ch_distributed_table_pair_forwards_wait_and_retry_options(
    lifecycle_adapter: LifecycleAdapter,
    connection_key: str | None,
    expected_retry: bool,
) -> None:
    maintenance.drop_ch_distributed_table_pair(
        "connection",
        "db.target",
        connection_key=connection_key,
        wait_for_absence=True,
        wait_timeout_seconds=7,
        wait_poll_interval_seconds=0.25,
    )

    call = lifecycle_adapter.calls[0]
    assert call[0] == "drop_table_with_options"
    assert call[2]["connection_key"] == (connection_key or "")
    assert call[2]["ch_retry_per_host_drops"] is expected_retry
    assert call[2]["ch_wait_timeout_seconds"] == 7


def test_clear_ch_distributed_table_data_builds_and_executes_both_commands(
    lifecycle_adapter: LifecycleAdapter,
) -> None:
    maintenance.clear_ch_distributed_table_data(
        "connection",
        "db.target",
        ch_cluster="cluster",
        query_label="q",
    )

    assert [call[0] for call in lifecycle_adapter.calls] == [
        "build_clear_target_sqls",
        "execute_commands",
    ]
    assert lifecycle_adapter.calls[1][1][1] == [
        "TRUNCATE shard",
        "TRUNCATE distributed",
    ]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (" id ", ["id"]),
        ((" id ", "date"), ["id", "date"]),
    ],
)
def test_normalize_key_columns_accepts_supported_values(value: Any, expected: Any) -> None:
    assert table_validation.normalize_key_columns(value) == expected


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


def test_validate_key_columns_in_columns_handles_skip_success_and_missing() -> None:
    table_validation.validate_key_columns_in_columns(None, ["id"])
    table_validation.validate_key_columns_in_columns(["id"], ["id", "value"])

    with pytest.raises(ValueError, match="missing"):
        table_validation.validate_key_columns_in_columns(["id", "missing"], ["id"])


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, None), (" partition_date ", "partition_date")],
)
def test_normalize_upsert_partition_column_accepts_valid_values(
    value: Any,
    expected: str | None,
) -> None:
    assert table_validation.normalize_upsert_partition_column(value) == expected


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


def test_validate_upsert_partition_column_in_columns() -> None:
    table_validation.validate_upsert_partition_column_in_columns(None, [])
    table_validation.validate_upsert_partition_column_in_columns("date", ["date"])

    with pytest.raises(ValueError, match="missing"):
        table_validation.validate_upsert_partition_column_in_columns("missing", ["date"])


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


class ValidationAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def build_stage_duplicate_keys_sql_for_tables(
        self,
        stage_tables: Any,
        key_columns: Any,
    ) -> str:
        self.calls.append(("build", (stage_tables, key_columns)))
        return "duplicate query"

    def query_has_rows(self, connection: Any, sql: str) -> bool:
        self.calls.append(("query", (connection, sql)))
        return True

    def stage_has_duplicate_keys(self, *args: Any) -> bool:
        self.calls.append(("duplicate", args))
        return False

    def stage_keys_overlap_target(self, *args: Any) -> bool:
        self.calls.append(("overlap", args))
        return True

    def null_safe_key_equality(self, *args: Any) -> str:
        self.calls.append(("equality", args))
        return "left.id IS NOT DISTINCT FROM right.id"


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


@pytest.mark.parametrize(
    ("sql", "max_chars", "expected"),
    [(None, 10, None), (" SELECT   1 ", 20, "SELECT 1"), ("SELECT 123456", 10, "SELECT ...")],
)
def test_sql_preview_normalizes_and_truncates(
    sql: str | None,
    max_chars: int,
    expected: str | None,
) -> None:
    assert errors.sql_preview(sql, max_chars=max_chars) == expected


def test_sql_exception_context_annotation_and_operation_error() -> None:
    context = SqlOperationContext(
        operation="transfer",
        alias="target",
        backend="gp",
        phase="insert",
        target_table="schema.target",
        source_table="schema.source",
        retry_attempt=2,
        sql_preview="INSERT ...",
    )
    original = RuntimeError("failed")

    assert errors.annotate_sql_exception(original, context) is original
    assert original.sql_context is context
    assert "target_table=schema.target" in original.__notes__[0]

    wrapped = errors.operation_error(original, context)
    assert isinstance(wrapped, SqlOperationError)
    assert wrapped.context is context
    assert "alias=target" in str(wrapped)
    assert "backend=gp" in str(wrapped)
    assert "phase=insert" in str(wrapped)


def test_add_exception_note_supports_legacy_and_locked_exceptions() -> None:
    class LegacyError(Exception):
        add_note = None

    class LockedNotesError(Exception):
        add_note = None

        def __setattr__(self, name: str, value: Any) -> None:
            if name == "__notes__":
                message = "locked"
                raise RuntimeError(message)
            super().__setattr__(name, value)

    legacy = LegacyError("legacy")
    errors._add_exception_note(legacy, "context")
    errors._add_exception_note(LockedNotesError("locked"), "context")

    assert legacy.__notes__ == ["context"]


def test_operation_error_and_context_note_omit_missing_optional_fields() -> None:
    context = SqlOperationContext(operation="read")

    wrapped = errors.operation_error(ValueError("bad"), context)

    assert str(wrapped) == "SQL operation failed (read): ValueError: bad"
    assert errors._format_context_note(context) == "SQL context: operation=read"


class RowCountResult:
    def __init__(self, **values: Any) -> None:
        self.__dict__.update(values)


@pytest.mark.parametrize(
    ("executed", "expected"),
    [
        (RowCountResult(rowcount=3), 3),
        ({"writtenRows": "4"}, 4),
        (RowCountResult(summary={"processedRows": 5}), 5),
        (RowCountResult(written_rows=6), 6),
        (RowCountResult(writtenRows=7), 7),
        (RowCountResult(processed_rows=8), 8),
        (RowCountResult(rows=9), 9),
        (RowCountResult(), 0),
    ],
)
def test_extract_row_count_supports_backend_result_shapes(executed: Any, expected: int) -> None:
    assert backend_utils.extract_row_count(executed) == expected


def test_backend_sql_literal_helpers() -> None:
    assert backend_utils.user_filter("user_name", "current_user", None) == (
        "user_name = current_user"
    )
    assert backend_utils.user_filter("user_name", "current_user", "O'Reilly") == (
        "user_name = 'O''Reilly'"
    )
    assert backend_utils.sql_in_list("name", ["a", "b's"]) == "name in ('a', 'b''s')"
    with pytest.raises(ValueError, match="must not be empty"):
        backend_utils.sql_in_list("name", [])

    assert backend_utils.sql_literal(None) == "NULL"
    assert backend_utils.sql_literal(True) == "TRUE"
    assert backend_utils.sql_literal(False) == "FALSE"
    assert backend_utils.sql_literal(3) == "3"
    assert backend_utils.sql_literal(2.5) == "2.5"
    assert backend_utils.sql_literal(date(2026, 1, 2)) == "'2026-01-02'"
    assert (
        backend_utils.sql_literal(datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc))
        == "'2026-01-02T03:04:05+00:00'"
    )
    assert backend_utils.sql_literal("x'y") == "'x''y'"


@pytest.mark.parametrize(
    ("value", "expected"),
    [(True, None), (None, None), ("4", 4), (-1, None), ("bad", None)],
)
def test_backend_row_count_coercion(value: Any, expected: int | None) -> None:
    assert backend_utils._coerce_row_count(value) == expected


def test_clickhouse_option_facade_delegates_to_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
    adapter = SimpleNamespace(
        normalize_ch_columns_or_expression=lambda *args: (
            calls.append(("columns", args, {})) or ["id"]
        ),
        normalize_ch_string=lambda *args: calls.append(("string", args, {})) or "value",
        validate_ch_create_table_options=lambda **kwargs: calls.append(("options", (), kwargs)),
        validate_ch_columns_in_columns=lambda *args, **kwargs: calls.append(
            ("in_columns", args, kwargs)
        ),
    )
    monkeypatch.setattr(backends, "get_backend_adapter", lambda _backend: adapter)

    assert ch_options.normalize_ch_columns_or_expression("id", "order_by") == ["id"]
    assert ch_options.normalize_ch_string(" value ", "engine") == "value"
    ch_options.validate_ch_options_not_used(
        target_backend="gp",
        option_owner="load_df",
        partition_by=None,
        order_by=None,
        ch_engine="ReplicatedMergeTree",
        ch_cluster="cluster",
        ch_sharding_key="rand()",
    )
    ch_options.validate_ch_columns_in_columns(
        ["id"],
        ["id", "value"],
        "order_by",
        data_name="dataframe",
    )

    assert [call[0] for call in calls] == ["columns", "string", "options", "in_columns"]
