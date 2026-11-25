"""Scenarios skip before resource access when their backend is unavailable."""

from __future__ import annotations

import pytest

from tests.sql.integration._support import backends
from tests.sql.integration.core import audit_adaptive_snapshot_scope as scenario


def test_adaptive_snapshot_scenario_skips_before_io_without_greenplum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ATK_SQL_INTEGRATION_BACKENDS", "ch,trino")
    monkeypatch.setattr(backends, "backend_enabled", lambda backend: backend != "gp")
    # If the scenario has imported the helper, control that same availability
    # boundary too. Neither resource creation nor a real connection is allowed.
    monkeypatch.setattr(scenario, "backend_enabled", backends.backend_enabled, raising=False)

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Disabled-backend scenario attempted resource I/O")

    monkeypatch.setattr(scenario, "_source", forbidden)
    with pytest.raises(pytest.skip.Exception):
        scenario.test_changed_keyed_native_source_scope_cannot_publish_a_certified_checkpoint(
            object(), {}, forbidden, monkeypatch
        )
