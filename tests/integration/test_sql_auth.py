from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from analytics_toolkit import sql

if TYPE_CHECKING:
    from collections.abc import Callable


pytestmark = [pytest.mark.integration, pytest.mark.integration_auth]


def _auth_aliases() -> list[str]:
    aliases = ["trino_basic_tls", "ch_tls"]
    if os.environ.get("SQL_INTEGRATION_GP") == "1":
        aliases.append("gp_tls")
    return aliases


def test_tls_connections_validate_and_execute() -> None:
    aliases = _auth_aliases()
    parsed = sql.validate_connections(aliases, connect=False)
    connected = sql.validate_connections(aliases, connect=True)

    assert all(result.valid for result in parsed)
    assert all(result.valid and result.connected for result in connected)
    for alias in aliases:
        frame = sql.read(alias, "SELECT 1 AS value")
        assert int(frame.iloc[0, 0]) == 1


def test_real_airflow_connection_source_routes_all_backends(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, object]], Path],
) -> None:
    aliases = {
        "airflow_trino": {"type": "trino"},
        "airflow_ch": {"type": "ch"},
    }
    monkeypatch.setenv(
        "AIRFLOW_CONN_AIRFLOW_TRINO",
        "http://integration:integration@127.0.0.1:18080/iceberg",
    )
    monkeypatch.setenv(
        "AIRFLOW_CONN_AIRFLOW_CH",
        "http://integration:integration@127.0.0.1:18123/integration",
    )
    if os.environ.get("SQL_INTEGRATION_GP") == "1":
        aliases["airflow_gp"] = {"type": "gp"}
        monkeypatch.setenv(
            "AIRFLOW_CONN_AIRFLOW_GP",
            "postgresql://gpadmin:integration@127.0.0.1:15432/analytics_toolkit",
        )
    write_sql_connections({"source": "airflow", "connections": aliases})

    results = sql.validate_connections(list(aliases), connect=True)
    assert all(result.valid and result.connected for result in results)


def test_auth_environment_is_secret_free() -> None:
    realm_path = Path(__file__).parents[2] / "integration/keycloak/integration-realm.json"
    realm = json.loads(realm_path.read_text(encoding="utf-8"))
    assert realm["realm"] == "integration"
