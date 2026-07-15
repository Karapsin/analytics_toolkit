from __future__ import annotations

import os

import pytest
from analytics_toolkit import sql

pytestmark = [pytest.mark.integration, pytest.mark.integration_fault]


def test_fault_profile_starts_from_healthy_fresh_connections() -> None:
    aliases = ["trino", "ch"]
    if os.environ.get("SQL_INTEGRATION_GP") == "1":
        aliases.append("gp")
    results = sql.validate_connections(aliases, connect=True)
    assert all(result.valid and result.connected for result in results)
