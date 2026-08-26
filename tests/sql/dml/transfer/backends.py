from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import Any

import pytest
from analytics_toolkit.general import logging as general_logging
from analytics_toolkit.sql.backends import adapter_defaults
from analytics_toolkit.sql.backends.ch import create_table_as as ch_create_table_as
from analytics_toolkit.sql.backends.ch import lifecycle as ch_lifecycle
from analytics_toolkit.sql.backends.ch import operations as ch_operations
from analytics_toolkit.sql.backends.ch import transfer_cleanup


def test_private_log_prefix_and_default_transfer_hooks_cover_empty_paths() -> None:
    assert general_logging._normalize_message_prefix(None) is None
    assert adapter_defaults.preclear_distributed_replace_target(object()) is False
    assert adapter_defaults.needs_bounded_replace_preclear(object(), False) is False
    with pytest.raises(RuntimeError, match="does not provide per-host"):
        adapter_defaults.open_transfer_host_connection(object(), "alias", "host")


def test_default_drop_table_delegates_to_adapter() -> None:
    calls: list[tuple[Any, str]] = []
    adapter = SimpleNamespace(
        drop_table_sql=lambda table_name, **options: f"DROP {table_name} {options['flag']}",
        execute_command=lambda connection, sql: calls.append((connection, sql)),
    )
    connection = object()

    adapter_defaults.drop_table(adapter, connection, "schema.table", flag="yes")

    assert calls == [(connection, "DROP schema.table yes")]


def test_clickhouse_transfer_cleanup_adapter_hooks(monkeypatch: pytest.MonkeyPatch) -> None:
    connection_module = importlib.import_module(
        "analytics_toolkit.sql.connection.get_sql_connection"
    )
    lifecycle_module = importlib.import_module("analytics_toolkit.sql.backends.ch.lifecycle")
    monkeypatch.setattr(
        connection_module,
        "get_ch_connection_for_host",
        lambda key, host: (key, host),
    )
    assert transfer_cleanup.open_transfer_host_connection(object(), "target", "host-a") == (
        "target",
        "host-a",
    )
    assert transfer_cleanup.needs_bounded_replace_preclear(object(), False) is True
    assert transfer_cleanup.needs_bounded_replace_preclear(object(), True) is False
    assert (
        transfer_cleanup.build_creation_policy_cleanup_sqls(
            object(),
            "db.table",
            None,
        )
        == []
    )

    monkeypatch.setattr(
        lifecycle_module,
        "build_drop_ch_creation_policy_table_sqls",
        lambda table, policy, **options: [table, policy, options],
    )
    policy = object()
    assert transfer_cleanup.build_creation_policy_cleanup_sqls(
        object(),
        "db.table",
        policy,
        query_label="label",
        if_exists=False,
    ) == ["db.table", policy, {"query_label": "label", "if_exists": False}]

    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        lifecycle_module,
        "drop_ch_distributed_table_pair_bounded",
        lambda *args, **kwargs: calls.append((*args, kwargs)),
    )
    assert (
        transfer_cleanup.preclear_distributed_replace_target(
            object(),
            "db.table",
            "cluster",
            only_shard=True,
            query_label=None,
            retry_per_host_drops=True,
            connection_runner=object(),
            host_connection_runner=object(),
        )
        is False
    )
    assert (
        transfer_cleanup.preclear_distributed_replace_target(
            object(),
            "db.table",
            "cluster",
            only_shard=False,
            query_label="label",
            retry_per_host_drops=True,
            connection_runner="connection-runner",
            host_connection_runner="host-runner",
        )
        is True
    )
    assert calls == [
        (
            "db.table",
            "cluster",
            {
                "query_label": "label",
                "ch_retry_per_host_drops": True,
                "connection_runner": "connection-runner",
                "host_connection_runner": "host-runner",
            },
        )
    ]


def test_clickhouse_cte_helpers_cover_filtered_and_fallback_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctes = [
        SimpleNamespace(args={"scalar": True}, alias="scalar"),
        SimpleNamespace(args={}, alias="   "),
        SimpleNamespace(args={}, alias="UsefulCte"),
        SimpleNamespace(args={}, alias="usefulcte"),
    ]
    monkeypatch.setattr(
        ch_create_table_as,
        "parse_one",
        lambda *_args, **_kwargs: SimpleNamespace(find_all=lambda _kind: ctes),
    )
    assert ch_create_table_as._extract_query_cte_names("query") == {"usefulcte": "UsefulCte"}

    class EmptyNamePattern:
        @staticmethod
        def finditer(_message: str) -> list[Any]:
            return [SimpleNamespace(group=lambda _name: '""')]

    monkeypatch.setattr(ch_create_table_as, "_CLICKHOUSE_MISSING_TABLE_RE", EmptyNamePattern())
    assert ch_create_table_as._clickhouse_missing_table_names(Exception("UNKNOWN_TABLE")) == []

    class NoNotes:
        __notes__: tuple[str, ...] = ()
        add_note = None

        def __setattr__(self, name: str, value: Any) -> None:
            if name == "__notes__":
                message = "notes locked"
                raise RuntimeError(message)
            super().__setattr__(name, value)

    ch_create_table_as._add_exception_note_once(NoNotes(), "note")  # type: ignore[arg-type]


def test_clickhouse_operation_lazy_helpers_and_invalid_drop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata_module = importlib.import_module("analytics_toolkit.sql.backends.ch.metadata")
    observed: list[Any] = []
    monkeypatch.setattr(
        metadata_module,
        "apply_clickhouse_shard_stats",
        lambda key, tables, *, read_sql: observed.append((key, tables, read_sql)) or tables,
    )
    tables = object()
    assert (
        ch_operations.postprocess_show_tables(
            object(),
            "target",
            tables,
            ch_distributed_table_stats=True,
        )
        is tables
    )
    assert observed[0][:2] == ("target", tables)
    assert callable(observed[0][2])

    with pytest.raises(ValueError, match="At least one"):
        ch_operations.build_drop_tables_sqls(
            object(),
            "db.table",
            ch_drop_shard=False,
            ch_drop_distributed=False,
        )
    assert ch_operations.build_drop_tables_sqls(
        object(),
        "db.table",
        ch_drop_shard=False,
        ch_drop_distributed=True,
    )
    assert ch_operations.build_drop_tables_sqls(
        object(),
        "db.table",
        ch_drop_shard=True,
        ch_drop_distributed=False,
    )

    wait_module = importlib.import_module("analytics_toolkit.sql.backends.ch.wait")
    waits: list[tuple[str, Any, str, Any]] = []
    monkeypatch.setattr(
        wait_module,
        "_wait_for_ch_table_absence",
        lambda connection, table: waits.append(("local", connection, table, None)),
    )
    monkeypatch.setattr(
        wait_module,
        "_wait_for_ch_table_absence_on_cluster",
        lambda connection, table, *, ch_cluster: waits.append(
            ("cluster", connection, table, ch_cluster)
        ),
    )
    connection = object()
    ch_operations.wait_for_table_absence(object(), connection, "db.table")
    ch_operations.wait_for_table_absence(
        object(),
        connection,
        "db.table",
        ch_cluster="cluster",
    )
    assert waits == [
        ("local", connection, "db.table", None),
        ("cluster", connection, "db.table", "cluster"),
    ]

    def invalid_table(*_args: Any, **_kwargs: Any) -> None:
        message = "bad table"
        raise ValueError(message)

    monkeypatch.setattr(ch_operations, "parse_one", invalid_table)
    assert ch_operations._is_default_ch_shard_table_name("not valid") is False
    monkeypatch.setattr(ch_operations, "parse_one", lambda *_args, **_kwargs: object())
    assert ch_operations._is_default_ch_shard_table_name("db.table") is False


def test_clickhouse_bounded_drop_timeout_and_host_error_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ch_lifecycle, "_execute_ch_sqls", lambda *_args: None)
    monkeypatch.setattr(
        ch_lifecycle,
        "_wait_for_ch_distributed_table_pair_absence",
        lambda *_args, **_kwargs: None,
    )

    def successful_runner(_role: str, operation: Any) -> Any:
        return operation(object())

    ch_lifecycle.drop_ch_distributed_table_pair_bounded(
        "db.table",
        "cluster",
        query_label=None,
        ch_retry_per_host_drops=True,
        connection_runner=successful_runner,
        host_connection_runner=lambda *_args: None,
    )

    def timed_out_runner(_role: str, _operation: Any) -> None:
        message = "cluster wait timed out"
        raise TimeoutError(message)

    with pytest.raises(TimeoutError, match="cluster wait timed out"):
        ch_lifecycle.drop_ch_distributed_table_pair_bounded(
            "db.table",
            "cluster",
            query_label=None,
            ch_retry_per_host_drops=False,
            connection_runner=timed_out_runner,
            host_connection_runner=lambda *_args: None,
        )
    with pytest.raises(TimeoutError, match="requires a non-null ch_cluster"):
        ch_lifecycle.drop_ch_distributed_table_pair_bounded(
            "db.table",
            None,
            query_label=None,
            ch_retry_per_host_drops=True,
            connection_runner=timed_out_runner,
            host_connection_runner=lambda *_args: None,
        )

    pair = ch_lifecycle.ch_distributed_table_pair("db.table")
    monkeypatch.setattr(
        ch_lifecycle,
        "_query_ch_configured_cluster_hosts",
        lambda *_args: [],
    )
    monkeypatch.setattr(
        ch_lifecycle,
        "_select_ch_hosts_for_local_drop",
        lambda *_args, **_kwargs: [],
    )
    with pytest.raises(TimeoutError, match="could not find any configured"):
        ch_lifecycle._resolve_bounded_drop_hosts(successful_runner, pair, "cluster")

    def host_runner(host: str, operation: Any) -> Any:
        if host == "host-b":
            message = "host unavailable"
            raise OSError(message)
        return operation(object())

    with pytest.raises(TimeoutError, match="host-b"):
        ch_lifecycle._drop_pair_on_bounded_hosts(
            ["host-a", "host-b"],
            pair,
            None,
            host_runner,
            2,
        )


def test_clickhouse_creation_policy_drop_executes_all_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sqls = ["DROP distributed", "DROP shard"]
    observed: list[Any] = []
    monkeypatch.setattr(
        ch_lifecycle,
        "build_drop_ch_creation_policy_table_sqls",
        lambda *_args, **_kwargs: sqls,
    )
    monkeypatch.setattr(
        ch_lifecycle,
        "_execute_ch_sqls",
        lambda connection, statements: observed.append((connection, statements)),
    )
    connection = object()

    ch_lifecycle.drop_ch_creation_policy_tables(
        connection,
        "db.table",
        object(),
    )

    assert observed == [(connection, sqls)]
