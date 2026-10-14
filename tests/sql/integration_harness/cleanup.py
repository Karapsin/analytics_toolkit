from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pandas as pd
import pytest
from analytics_toolkit.sql.connection import get_connection_config

from tests._support.paths import REPO_ROOT
from tests.sql._support.connection_config import FakeAirflowConnection, install_fake_airflow
from tests.sql.integration import conftest as integration_conftest
from tests.sql.integration._support import resources as integration_resources

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def _prepare_cleanup_registry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    write_sql_connections: Callable[[dict[str, object]], Path],
    driver: str,
) -> integration_resources.ResourceRegistry:
    monkeypatch.setenv("SQL_INTEGRATION_PROFILE", "auth")
    monkeypatch.setenv("SQL_INTEGRATION_GP", "1")
    monkeypatch.setenv("SQL_INTEGRATION_CLICKHOUSE_DRIVER", driver)
    monkeypatch.setenv("SQL_INTEGRATION_CERTS", str(tmp_path))
    monkeypatch.delenv("SQL_INTEGRATION_COMPOSE_PROJECT", raising=False)
    monkeypatch.delenv("SQL_INTEGRATION_ARTIFACT_DIR", raising=False)
    integration_conftest._activate_integration_connections(write_sql_connections)
    monkeypatch.setattr(integration_resources, "RESOURCE_CLEANUP_ATTEMPTS", 1)
    registry = integration_resources.ResourceRegistry(
        root=REPO_ROOT, project=None, artifact_dir=tmp_path
    )
    registry.table("trino", "iceberg.integration.owned")
    registry.query("ch", "owned-query")
    return registry


@pytest.mark.parametrize("driver", ["http", "native"])
@pytest.mark.parametrize("leak", [False, True])
def test_cleanup_reactivates_cached_airflow_source_before_registry_and_scans(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    write_sql_connections: Callable[[dict[str, object]], Path],
    driver: str,
    leak: bool,
) -> None:
    registry = _prepare_cleanup_registry(monkeypatch, tmp_path, write_sql_connections, driver)
    cleaned: list[tuple[str, str]] = []
    scanned: list[tuple[str, str]] = []

    def check_config(key: str) -> None:
        config = get_connection_config(key)
        assert config.host == "127.0.0.1"
        assert config.backend == key
        if key == "ch":
            assert config.driver == driver

    def drop(key: str, table: str, **_kwargs) -> None:
        check_config(key)
        cleaned.append((key, table))

    def cancel(key: str, query_ids, **_kwargs) -> None:
        check_config(key)
        cleaned.append((key, query_ids[0]))

    def show_tables(key: str) -> pd.DataFrame:
        check_config(key)
        scanned.append((key, "tables"))
        return pd.DataFrame(
            [
                {
                    "db": "iceberg",
                    "schema": "integration",
                    "table_name": "it_stage_leaked" if leak else "unrelated",
                }
            ]
        )

    def show_queries(key: str, **_kwargs) -> pd.DataFrame:
        check_config(key)
        scanned.append((key, "queries"))
        return pd.DataFrame()

    monkeypatch.setattr(integration_conftest.sql, "drop_tables", drop)
    monkeypatch.setattr(integration_conftest.sql, "cancel_queries", cancel)
    monkeypatch.setattr(integration_conftest.sql, "show_tables", show_tables)
    monkeypatch.setattr(integration_conftest.sql, "show_queries", show_queries)
    monkeypatch.setattr(
        integration_conftest.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("external programs must not run"),
    )
    fixture = integration_conftest.assert_no_toolkit_leaks.__wrapped__(
        request=SimpleNamespace(node=SimpleNamespace()),
        default_sql_connections=None,
        resource_registry=registry,
        tmp_path=tmp_path,
        write_sql_connections=write_sql_connections,
    )
    next(fixture)
    install_fake_airflow(
        monkeypatch,
        {
            "airflow_trino": FakeAirflowConnection(
                conn_type="trino",
                host="127.0.0.1",
                port=18080,
                login="integration",
                schema="integration",
                extra_dejson={"catalog": "iceberg"},
            ),
        },
    )
    write_sql_connections(
        {"source": "airflow", "connections": {"airflow_trino": {"type": "trino"}}}
    )
    integration_conftest.general_module.set_connections_path(tmp_path / ".connections")
    assert get_connection_config("airflow_trino").catalog == "iceberg"

    if leak:
        with pytest.raises(pytest.fail.Exception, match="integration cleanup failed"):
            next(fixture)
    else:
        with pytest.raises(StopIteration):
            next(fixture)

    assert cleaned[:2] == [("ch", "owned-query"), ("trino", "iceberg.integration.owned")]
    assert scanned == [
        (key, operation)
        for key in ("trino", "ch", "gp")
        for operation in ("tables", "tables", "queries")
    ]
    report = json.loads((tmp_path / "integration-leaks.json").read_text(encoding="utf-8"))
    assert report["tables"] == (
        [f"{key}:it_stage_leaked" for key in ("trino", "ch", "gp")] if leak else []
    )
    assert report["queries"] == []
    assert report["objects"] == []
