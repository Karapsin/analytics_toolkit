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


@pytest.mark.parametrize("restart_fails", [False, True])
def test_explicit_restart_satisfies_only_successful_pending_recovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    restart_fails: bool,
) -> None:
    actions: list[tuple[str, ...]] = []
    restarts = 0

    def run(command, **_kwargs) -> subprocess.CompletedProcess[str]:
        nonlocal restarts
        args = tuple(command[command.index("auth") + 1 :])
        if args == ("config", "--services"):
            return subprocess.CompletedProcess(command, 0, "keycloak trino minio", "")
        actions.append(args)
        if args == ("restart", "keycloak"):
            restarts += 1
            if restart_fails and restarts == 1:
                return subprocess.CompletedProcess(command, 1, "", "restart failed")
        return subprocess.CompletedProcess(command, 0, '{"Health":"healthy"}', "")

    monkeypatch.setattr(integration_faults.subprocess, "run", run)
    controller = integration_faults.FaultController(
        root=tmp_path, project="disposable", artifact_dir=tmp_path
    )
    controller.stop("keycloak")
    controller.stop("trino")
    controller.pause("minio")
    if restart_fails:
        with pytest.raises(RuntimeError, match="restart failed"):
            controller.restart("keycloak")
        assert ("restart", "keycloak") in controller.restore_actions
    else:
        controller.restart("keycloak")
        controller.wait_healthy("keycloak")
        assert ("restart", "keycloak") not in controller.restore_actions
    controller.restore()

    assert restarts == (2 if restart_fails else 1)
    expected_restore = [("unpause", "minio"), ("restart", "trino")]
    if restart_fails:
        expected_restore.append(("restart", "keycloak"))
    assert actions[-len(expected_restore) :] == expected_restore
    assert controller.restore_actions == []


def test_fault_timeline_retains_scenarios_without_repeating_events(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    for scenario in ("discovery", "exchange", "empty"):
        monkeypatch.setenv("SQL_INTEGRATION_TEST_ID", scenario)
        controller = integration_faults.FaultController(
            root=tmp_path, project="disposable", artifact_dir=tmp_path
        )
        if scenario != "empty":
            controller._record("restart", "keycloak", True)
            controller.write_timeline()
            controller._record("healthy", "keycloak", True)
            controller.write_timeline()
        controller.restore()
        controller.write_timeline()

    timeline = json.loads((tmp_path / "fault-timeline.json").read_text(encoding="utf-8"))
    assert [(event["scenario_id"], event["action"]) for event in timeline] == [
        ("discovery", "restart"),
        ("discovery", "healthy"),
        ("exchange", "restart"),
        ("exchange", "healthy"),
    ]


def test_restore_attempts_every_action_after_recovery_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    actions: list[tuple[str, str]] = []

    def act(action: str, service: str) -> None:
        actions.append((action, service))
        if service == "keycloak":
            message = "restart failed"
            raise RuntimeError(message)

    controller = integration_faults.FaultController(
        root=tmp_path, project="disposable", artifact_dir=tmp_path
    )
    controller.restore_actions = [("restart", "trino"), ("restart", "keycloak")]
    monkeypatch.setattr(controller, "_act", act)
    with pytest.raises(RuntimeError, match="restart failed"):
        controller.restore()
    assert actions == [("restart", "keycloak"), ("restart", "trino")]
    assert controller.restore_actions == []
