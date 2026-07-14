from __future__ import annotations

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
