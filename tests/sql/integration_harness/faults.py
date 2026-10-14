from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING

import pytest

from release_routines import sql_integration
from tests.sql.integration import conftest as integration_conftest
from tests.sql.integration._support import faults as integration_faults

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("include_greenplum", [False, True])
@pytest.mark.parametrize("driver", ["http", "native"])
def test_fault_fixture_uses_startup_profiles_for_guard_actions_and_restore(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    include_greenplum: bool,
    driver: str,
) -> None:
    project = f"{sql_integration.PROJECT_NAME}-fault-{driver}"
    monkeypatch.setenv("SQL_INTEGRATION_PROFILE", "fault")
    monkeypatch.setenv("SQL_INTEGRATION_GP", "1" if include_greenplum else "0")
    monkeypatch.setenv("SQL_INTEGRATION_COMPOSE_PROJECT", project)
    monkeypatch.setenv("SQL_INTEGRATION_ARTIFACT_DIR", str(tmp_path))
    calls: list[tuple[str, ...]] = []
    expected_prefix = sql_integration._compose_command(
        include_greenplum=include_greenplum,
        profile="fault",
        clickhouse_driver=driver,
    )

    def run(command, **kwargs) -> subprocess.CompletedProcess[str]:
        assert command[: len(expected_prefix)] == expected_prefix
        assert kwargs["cwd"] == sql_integration.REPO_ROOT
        args = tuple(command[len(expected_prefix) :])
        calls.append(args)
        if args == ("config", "--services"):
            services = ["clickhouse", "trino"]
            if "--profile" in command and "gp" in command:
                services.append("greenplum")
            return subprocess.CompletedProcess(command, 0, "\n".join(services), "")
        if args == ("ps", "--format", "json", service):
            return subprocess.CompletedProcess(command, 0, '{"Health":"healthy"}', "")
        assert args in {("stop", service), ("restart", service)}
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(integration_faults.subprocess, "run", run)
    fixture = integration_conftest.fault_controller.__wrapped__()
    controller = next(fixture)
    service = "greenplum" if include_greenplum else "clickhouse"
    try:
        controller.stop(service)
        controller.wait_healthy(service)
        with pytest.raises(ValueError, match="outside the integration Compose project"):
            controller.stop("shared-database")
        if not include_greenplum:
            with pytest.raises(ValueError, match="outside the integration Compose project"):
                controller.stop("greenplum")
    finally:
        with pytest.raises(StopIteration):
            next(fixture)

    actions = [call for call in calls if call[0] != "config"]
    assert actions == [
        ("stop", service),
        ("ps", "--format", "json", service),
        ("restart", service),
    ]
    assert controller.restore_actions == []
    timeline = json.loads((tmp_path / "fault-timeline.json").read_text(encoding="utf-8"))
    assert [(item["action"], item["service"], item["ok"]) for item in timeline] == [
        ("stop", service, True),
        ("healthy", service, True),
        ("restart", service, True),
    ]
