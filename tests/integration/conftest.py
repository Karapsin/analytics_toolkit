from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

import pytest
from analytics_toolkit import sql

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path


LOOPBACK_HOSTS = {"127.0.0.1", "localhost"}


def _integration_connections() -> dict[str, dict[str, object]]:
    connections: dict[str, dict[str, object]] = {
        "trino": {
            "type": "trino",
            "host": "127.0.0.1",
            "port": int(os.environ.get("SQL_INTEGRATION_TRINO_PORT", "18080")),
            "user": "integration",
            "catalog": "iceberg",
            "schema": "integration",
            "http_scheme": "http",
            "verify": False,
            "insert_chunk_size": 100,
            "transfer_staging_schema": "iceberg.integration_stage",
            "transfer_staging_location": "s3://warehouse/staging",
            "upsert_partition_drop_sql_template": (
                "DELETE FROM {table} WHERE {partition_column} = {partition_value}"
            ),
        },
        "ch": {
            "type": "ch",
            "host": "127.0.0.1",
            "port": int(os.environ.get("SQL_INTEGRATION_CLICKHOUSE_PORT", "18123")),
            "user": "integration",
            "password": "integration",
            "database": "integration",
            "secure": False,
            "transfer_staging_schema": "integration",
        },
    }
    connections["trino_values"] = {
        **connections["trino"],
        "transfer_staging_location": None,
        "insert_chunk_size": 2,
    }
    connections["trino_parquet"] = {**connections["trino"]}
    connections["ch_limited"] = {
        **connections["ch"],
        "query_limit": 2,
        "query_retries": 1,
        "client_name": "analytics-toolkit-integration",
        "settings": {"max_execution_time": 60},
    }
    if os.environ.get("SQL_INTEGRATION_GP") == "1":
        connections["gp"] = {
            "type": "gp",
            "host": "127.0.0.1",
            "port": int(os.environ.get("SQL_INTEGRATION_GREENPLUM_PORT", "15432")),
            "user": "gpadmin",
            "password": "integration",
            "database": "analytics_toolkit",
            "sslmode": "disable",
            "transfer_staging_schema": "public",
        }
        connections["gp_alias"] = {**connections["gp"]}
    if os.environ.get("SQL_INTEGRATION_PROFILE") == "auth":
        certs = os.environ["SQL_INTEGRATION_CERTS"]
        connections["trino_basic_tls"] = {
            **connections["trino_values"],
            "port": int(os.environ.get("SQL_INTEGRATION_TRINO_TLS_PORT", "18443")),
            "password": "integration",
            "auth_mode": "basic",
            "http_scheme": "https",
            "ca_certs": [f"{certs}/ca.crt"],
            "verify": True,
        }
        connections["ch_tls"] = {
            **connections["ch"],
            "port": int(os.environ.get("SQL_INTEGRATION_CLICKHOUSE_TLS_PORT", "18444")),
            "secure": True,
            "verify": True,
            "ca_certs": [f"{certs}/ca.crt"],
        }
        if "gp" in connections:
            connections["gp_tls"] = {
                **connections["gp"],
                "port": int(os.environ.get("SQL_INTEGRATION_GP_TLS_PORT", "19432")),
                "sslmode": "verify-full",
                "ca_certs": [f"{certs}/ca.crt"],
                "ssl_cert": f"{certs}/client.crt",
                "ssl_key": f"{certs}/client.key",
            }
    return connections


def _assert_loopback_connections(connections: dict[str, dict[str, object]]) -> None:
    for key, config in connections.items():
        host = config.get("host")
        if host not in LOOPBACK_HOSTS:
            message = f"Integration connection {key!r} is not loopback-only: {host!r}"
            raise RuntimeError(message)


@pytest.fixture(autouse=True)
def default_sql_connections(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    if os.environ.get("ANALYTICS_TOOLKIT_RUN_INTEGRATION") != "1":
        pytest.skip("integration tests require the repository integration workflow")
    connections = _integration_connections()
    _assert_loopback_connections(connections)
    monkeypatch.chdir(tmp_path)
    write_sql_connections(connections)


@pytest.fixture(autouse=True)
def initialize_integration_schemas(default_sql_connections: None) -> Iterator[None]:
    del default_sql_connections
    if os.environ.get("ANALYTICS_TOOLKIT_RUN_INTEGRATION") != "1":
        yield
        return
    sql.execute("trino", "CREATE SCHEMA IF NOT EXISTS iceberg.integration")
    sql.execute("trino", "CREATE SCHEMA IF NOT EXISTS iceberg.integration_stage")
    yield


@pytest.fixture(autouse=True)
def assert_no_toolkit_leaks(
    default_sql_connections: None,
    tmp_path: Path,
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> Iterator[None]:
    del default_sql_connections
    yield
    write_sql_connections(_integration_connections())
    leak_report = {
        "tables": [],
        "queries": [],
        "objects": [],
    }
    for backend in ("trino", "ch"):
        tables = sql.show_tables(backend)
        names = tables.get("table_name", []).tolist()
        leak_report["tables"].extend(
            f"{backend}:{name}" for name in names if str(name).startswith("it_stage_")
        )
        active = sql.show_queries(backend, state="active")
        if "query" in active:
            leak_report["queries"].extend(
                f"{backend}:{value}"
                for value in active["query"].astype(str)
                if "analytics_toolkit_integration" in value
            )
    report_path = tmp_path / "integration-leaks.json"
    report_path.write_text(json.dumps(leak_report, indent=2), encoding="utf-8")
    assert not any(leak_report.values()), leak_report
