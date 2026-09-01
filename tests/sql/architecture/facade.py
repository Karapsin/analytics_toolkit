from __future__ import annotations

import importlib
import inspect
from collections.abc import Callable
from typing import Any

import pandas as pd
import pytest
from analytics_toolkit import sql

config_module = importlib.import_module("analytics_toolkit.sql.connection.config")
ddl_api_module = importlib.import_module("analytics_toolkit.sql.ddl.api")
drop_tables_module = importlib.import_module("analytics_toolkit.sql.dml.table.drop_tables")
execute_sql_module = importlib.import_module("analytics_toolkit.sql.dml.io.execute_sql")
load_df_module = importlib.import_module("analytics_toolkit.sql.dml.load.load_df")
partitions_module = importlib.import_module("analytics_toolkit.sql.dml.table.partitions")
transfer_api_module = importlib.import_module("analytics_toolkit.sql.dml.transfer.flow.api")


def _make_ch_config(connection_key: str) -> Any:
    return config_module.ChConfig(
        connection_key=connection_key,
        backend="ch",
        host="ch.example",
        port=8123,
        user="source_user",
        password="password",
        database="default",
        secure=False,
        verify_value=None,
        ca_certs=[],
        ca_certs_variable=None,
        connect_timeout=None,
        send_receive_timeout=None,
        settings=None,
        interface=None,
        query_limit=None,
        query_retries=None,
        client_name=None,
        transfer_staging_schema=None,
    )


def _make_gp_config(connection_key: str) -> Any:
    return config_module.GpConfig(
        connection_key=connection_key,
        backend="gp",
        host="gp.example",
        port=5432,
        user="source_user",
        password="password",
        database="db",
        connect_timeout=30,
        keepalives=True,
        keepalives_idle=60,
        keepalives_interval=10,
        keepalives_count=3,
        sslmode=None,
        ca_certs=[],
        ssl_cert=None,
        ssl_key=None,
        transfer_staging_schema=None,
    )


def _make_trino_config(connection_key: str) -> Any:
    return config_module.TrinoConfig(
        connection_key=connection_key,
        backend="trino",
        host="trino.example",
        port=8080,
        user="target_user",
        password="password",
        catalog="iceberg",
        schema="sandbox",
        auth_mode="basic",
        http_scheme="https",
        verify_value="true",
        ca_certs=[],
        insert_chunk_size=None,
        request_timeout=None,
        source=None,
        transfer_staging_schema=None,
        s3_transfer_staging_location=None,
    )


def _install_connection_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_get_sql_connection(db_key: str) -> None:
        pytest.fail(f"public dry-run smoke opened SQL connection {db_key!r}")

    def fail_get_clickhouse_client(db_key: str) -> None:
        pytest.fail(f"public dry-run smoke opened ClickHouse client {db_key!r}")

    for module in (
        ddl_api_module,
        drop_tables_module,
        execute_sql_module,
        load_df_module,
        partitions_module,
    ):
        if hasattr(module, "get_sql_connection"):
            monkeypatch.setattr(module, "get_sql_connection", fail_get_sql_connection)
        if hasattr(module, "get_clickhouse_client"):
            monkeypatch.setattr(
                module,
                "get_clickhouse_client",
                fail_get_clickhouse_client,
            )


def _install_transfer_configs(monkeypatch: pytest.MonkeyPatch) -> None:
    configs = {
        "ch": _make_ch_config("ch"),
        "gp": _make_gp_config("gp"),
        "trino": _make_trino_config("trino"),
    }
    monkeypatch.setattr(
        transfer_api_module,
        "get_connection_config",
        lambda db_key: configs[db_key],
    )


PUBLIC_DRY_RUN_OPERATIONS = {
    "create_table",
    "drop_partitions",
    "drop_tables",
    "execute",
    "execute_create",
    "execute_insert",
    "gp_create_partitions",
    "load_df",
    "insert",
    "transfer",
}


PUBLIC_DRY_RUN_SMOKE_CASES: dict[str, Callable[[], Any]] = {
    "create_table": lambda: sql.create_table(
        "gp",
        "sandbox.schema_only",
        table_schema={"id": "BIGINT"},
        dry_run=True,
    ),
    "drop_partitions": lambda: sql.drop_partitions(
        "gp",
        "sandbox.events",
        ["2025-05-01"],
        dry_run=True,
    ),
    "drop_tables": lambda: sql.drop_tables(
        "ch",
        "sandbox.events",
        dry_run=True,
    ),
    "execute": lambda: sql.execute(
        "ch",
        "select 1",
        dry_run=True,
    ),
    "execute_create": lambda: sql.execute_create(
        "gp", "sandbox.created", "SELECT 1 AS id", dry_run=True
    ),
    "execute_insert": lambda: sql.execute_insert(
        "gp", "sandbox.events", "SELECT 1 AS id", dry_run=True
    ),
    "gp_create_partitions": lambda: sql.gp_create_partitions(
        "gp",
        "sandbox.events",
        days=["2026-05-01"],
        dry_run=True,
    ),
    "load_df": lambda: sql.load_df(
        "gp",
        "sandbox.target",
        pd.DataFrame({"id": [1]}),
        dry_run=True,
    ),
    "insert": lambda: sql.insert("gp", "sandbox.events", "SELECT 1 AS id", dry_run=True),
    "transfer": lambda: sql.transfer(
        from_db="ch",
        to_db="trino",
        from_sql="select 1 as id",
        to_table="iceberg.sandbox.target",
        table_schema={"id": "BIGINT"},
        trino_mode="values",
        dry_run=True,
    ),
}


def _call_public_smoke(function_name: str) -> Any:
    try:
        return PUBLIC_DRY_RUN_SMOKE_CASES[function_name]()
    except TypeError as exc:
        message = str(exc)
        if "missing" in message and "required" in message and "argument" in message:
            pytest.fail(
                f"public sql.{function_name} raised a missing-argument TypeError: {message}"
            )
        raise


def test_public_dry_run_smoke_cases_cover_mutating_sql_facade() -> None:
    assert set(PUBLIC_DRY_RUN_SMOKE_CASES) == PUBLIC_DRY_RUN_OPERATIONS

    missing_dry_run = [
        name
        for name in sorted(PUBLIC_DRY_RUN_OPERATIONS)
        if "dry_run" not in inspect.signature(sql.__dict__[name]).parameters
    ]
    assert missing_dry_run == []


@pytest.mark.parametrize("function_name", sorted(PUBLIC_DRY_RUN_SMOKE_CASES))
def test_public_dry_run_sql_facade_smoke_returns_plan(
    function_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_connection_guards(monkeypatch)
    _install_transfer_configs(monkeypatch)

    result = _call_public_smoke(function_name)

    assert isinstance(result, sql.SqlPlan)


def test_public_transfer_dry_run_accepts_from_table_without_placeholders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_connection_guards(monkeypatch)
    _install_transfer_configs(monkeypatch)

    plan = sql.transfer(
        from_db="ch",
        to_db="trino",
        from_table="dm_nrt.loyalty_events",
        to_table="iceberg.sandbox.target",
        table_schema={"id": "BIGINT"},
        trino_mode="values",
        dry_run=True,
    )

    read_source_sqls = [
        statement.sql for statement in plan.statements if statement.phase == "read_source"
    ]
    assert read_source_sqls == ["SELECT * FROM dm_nrt.loyalty_events"]


def test_public_transfer_dry_run_renders_keyed_placeholders_inline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_connection_guards(monkeypatch)
    _install_transfer_configs(monkeypatch)

    plan = sql.transfer(
        from_db="ch",
        to_db="trino",
        from_sql=(
            "select event_date as dt, store_id, product_id as article_id "
            "from dm_nrt.loyalty_events "
            "where event_name = 'catalog_itemScreen_productListing_item_view' "
            "and {event_date} and ({event_date})"
        ),
        to_table="iceberg.sandbox.target",
        table_schema={"dt": "DATE", "store_id": "BIGINT", "article_id": "BIGINT"},
        transfer_keys="event_date",
        transfer_key_values=["2026-04-01", "2026-04-02"],
        concurrency=2,
        trino_mode="values",
        dry_run=True,
    )

    read_source_sqls = [
        statement.sql for statement in plan.statements if statement.phase == "read_source"
    ]
    assert len(read_source_sqls) == 2
    assert all("analytics_toolkit_transfer_source" not in text for text in read_source_sqls)
    assert read_source_sqls[0].count("(event_date) = '2026-04-01'") == 2
    assert read_source_sqls[1].count("(event_date) = '2026-04-02'") == 2
    assert all("{event_date}" not in text for text in read_source_sqls)


def test_public_transfer_dry_run_renders_keyed_from_table_predicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_connection_guards(monkeypatch)
    _install_transfer_configs(monkeypatch)

    plan = sql.transfer(
        from_db="ch",
        to_db="trino",
        from_table="dm_nrt.loyalty_events",
        to_table="iceberg.sandbox.target",
        table_schema={"dt": "DATE"},
        transfer_keys={"event_dt": "event_date"},
        transfer_key_values=["2026-04-01", "2026-04-02"],
        concurrency=2,
        trino_mode="values",
        dry_run=True,
    )

    read_source_sqls = [
        statement.sql for statement in plan.statements if statement.phase == "read_source"
    ]
    assert read_source_sqls == [
        "SELECT * FROM dm_nrt.loyalty_events\nWHERE (event_date) = '2026-04-01'",
        "SELECT * FROM dm_nrt.loyalty_events\nWHERE (event_date) = '2026-04-02'",
    ]
