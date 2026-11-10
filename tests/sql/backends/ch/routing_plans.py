"""Cluster routing in dry runs and generated SQL plans."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.routing import prepare_plan_sql
from analytics_toolkit.sql.connection.errors import UnsupportedConnectionTypeError
from analytics_toolkit.sql.execution.plans import SqlPlan

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import pytest

execute_sql_module = importlib.import_module("analytics_toolkit.sql.dml.io.execute_sql")
config_module = importlib.import_module("analytics_toolkit.sql.connection.config")


def test_execute_sql_dry_run_contains_routed_sql(
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections(
        {
            "routed": {
                "type": "ch",
                "host": "ch.example",
                "user": "user",
                "password": "password",
                "database": "analytics",
                "cluster_routing": {"cluster": "core"},
            }
        }
    )

    plan = execute_sql_module.execute_sql(
        "routed",
        "SELECT * FROM events",
        dry_run=True,
    )

    assert plan.sqls == ["SELECT * FROM cluster('core', 'analytics', 'events')"]


def test_generated_plan_sql_is_routed_and_keeps_local_create_fallback(
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections(
        {
            "routed": {
                "type": "ch",
                "host": "ch.example",
                "user": "user",
                "password": "password",
                "database": "analytics",
                "cluster_routing": {"cluster": "core"},
            }
        }
    )
    plan = SqlPlan(operation="generated")
    plan.add(
        "CREATE TABLE analytics.events (id UInt8) ENGINE=Memory",
        alias="routed",
        backend="ch",
    )
    plan.add(
        "CREATE TABLE analytics.shard ON CLUSTER '{cluster}' (id UInt8) ENGINE=Memory",
        alias="routed",
        backend="ch",
    )
    plan.add(
        "CREATE TABLE IF NOT EXISTS analytics.shard (id UInt8) ENGINE=Memory",
        alias="routed",
        backend="ch",
    )
    plan.add(
        "INSERT INTO analytics.events VALUES (1)",
        alias="routed",
        backend="ch",
    )

    assert "ON CLUSTER 'core'" in plan.sqls[0]
    assert "ON CLUSTER '{cluster}'" in plan.sqls[1]
    assert "ON CLUSTER" not in plan.sqls[2]
    assert plan.sqls[3] == (
        "INSERT INTO FUNCTION cluster('core', 'analytics', 'events', rand()) VALUES ((1))"
    )
    assert (
        prepare_plan_sql(
            get_backend_adapter("ch"),
            "routed",
            "INSERT INTO analytics.events VALUES <1 dataframe row>",
            [],
        )
        == "INSERT INTO analytics.events VALUES <1 dataframe row>"
    )


def test_plan_without_connection_metadata_keeps_sql_unchanged() -> None:
    plan = SqlPlan(operation="local")

    plan.add("SELECT 1")

    assert plan.sqls == ["SELECT 1"]


def test_prepare_plan_sql_ignores_unsupported_connection_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _unsupported(_key: str) -> None:
        message = "unsupported"
        raise UnsupportedConnectionTypeError(message)

    monkeypatch.setattr(config_module, "get_connection_config", _unsupported)

    assert prepare_plan_sql(get_backend_adapter("ch"), "unsupported", "SELECT 1", []) == "SELECT 1"
