from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from analytics_toolkit import sql

if TYPE_CHECKING:
    from tests.integration.support.faults import FaultController

pytestmark = [pytest.mark.integration, pytest.mark.integration_fault]


@pytest.mark.sql_scenario("fault.connections.fresh")
def test_fault_profile_starts_from_healthy_fresh_connections() -> None:
    aliases = ["trino", "ch"]
    if os.environ.get("SQL_INTEGRATION_GP") == "1":
        aliases.append("gp")
    results = sql.validate_connections(aliases, connect=True)
    assert all(result.valid and result.connected for result in results)


@pytest.mark.sql_scenario("fault.database.clickhouse_restart")
def test_database_fault_restart_recovers(
    fault_controller: FaultController,
) -> None:
    if os.environ.get("SQL_INTEGRATION_FAULT_GROUP") not in {None, "database"}:
        pytest.skip("database fault group not selected")
    fault_controller.restart("clickhouse")
    fault_controller.wait_healthy("clickhouse")
    result = sql.validate_connections(["ch"], connect=True)[0]
    assert result.valid
    assert result.connected


@pytest.mark.sql_scenario("fault.staging.minio_pause")
def test_staging_fault_is_always_restored(
    fault_controller: FaultController,
) -> None:
    if os.environ.get("SQL_INTEGRATION_FAULT_GROUP") not in {None, "staging"}:
        pytest.skip("staging fault group not selected")
    fault_controller.pause("minio")
    fault_controller.unpause("minio")
    fault_controller.wait_healthy("minio")


@pytest.mark.sql_scenario("fault.authentication.keycloak_restart")
def test_authentication_fault_is_project_scoped(
    fault_controller: FaultController,
) -> None:
    if os.environ.get("SQL_INTEGRATION_FAULT_GROUP") not in {None, "authentication"}:
        pytest.skip("authentication fault group not selected")
    fault_controller.restart("keycloak")
    assert fault_controller.timeline[-1]["service"] == "keycloak"
