from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest

from release_routines import sql_integration
from tests._support.paths import REPO_ROOT

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("profile", ["core", "auth", "fault", "stress"])
@pytest.mark.parametrize(
    ("http_result", "native_result", "expected"),
    [(17, 0, 17), (0, 23, 23), (17, 23, 17)],
)
def test_both_transports_run_after_failure_and_keep_first_error(
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
    http_result: int,
    native_result: int,
    expected: int,
) -> None:
    calls: list[tuple[str, str]] = []

    def run_profile(**kwargs) -> int:
        calls.append((kwargs["profile"], kwargs["clickhouse_driver"]))
        return http_result if kwargs["clickhouse_driver"] == "http" else native_result

    monkeypatch.setattr(sql_integration, "run_profile", run_profile)
    monkeypatch.setattr(
        sql_integration,
        "_assert_transport_scenario_parity",
        lambda **_kwargs: pytest.fail("failed runs must not report collection parity"),
    )

    assert (
        sql_integration.run(
            profile=profile,
            include_greenplum=True,
            clickhouse_driver="both",
        )
        == expected
    )
    assert calls == [(profile, "http"), (profile, "native")]


@pytest.mark.parametrize("failed_profile", ["core", "auth", "fault", "stress"])
def test_all_stops_after_failed_profile_but_runs_its_native_transport(
    monkeypatch: pytest.MonkeyPatch,
    failed_profile: str,
) -> None:
    calls: list[tuple[str, str]] = []
    parity: list[str] = []

    def run_profile(**kwargs) -> int:
        profile = kwargs["profile"]
        driver = kwargs["clickhouse_driver"]
        calls.append((profile, driver))
        assert kwargs["include_greenplum"] is True
        assert kwargs["fault_group"] == "authentication"
        return 17 if profile == failed_profile and driver == "http" else 0

    def check_parity(**kwargs) -> int:
        parity.append(kwargs["profile"])
        return 0

    monkeypatch.setattr(sql_integration, "run_profile", run_profile)
    monkeypatch.setattr(sql_integration, "_assert_transport_scenario_parity", check_parity)

    assert (
        sql_integration.run(
            profile="all",
            include_greenplum=True,
            fault_group="authentication",
            clickhouse_driver="both",
        )
        == 17
    )
    profiles = ["core", "auth", "fault", "stress"]
    completed = profiles[: profiles.index(failed_profile)]
    assert calls == [
        (profile, driver)
        for profile in [*completed, failed_profile]
        for driver in ("http", "native")
    ]
    assert parity == completed


def test_failed_startup_still_creates_both_transport_logs_and_tears_down(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    teardown: list[list[str]] = []

    def capture(path: Path, _command) -> int:
        path.write_text("service image could not be pulled\n", encoding="utf-8")
        return 17

    def run_command(command, **_kwargs) -> int:
        teardown.append(command)
        return 0

    monkeypatch.setattr(sql_integration, "ARTIFACTS_DIR", tmp_path)
    monkeypatch.setattr(sql_integration.importlib.util, "find_spec", lambda _name: object())
    monkeypatch.setattr(sql_integration, "_capture", capture)
    monkeypatch.setattr(sql_integration, "_run", run_command)
    monkeypatch.setattr(sql_integration, "_write_diagnostics", lambda **_kwargs: None)
    monkeypatch.setattr(sql_integration, "_assert_teardown_clean", lambda **_kwargs: 0)
    monkeypatch.setattr(
        sql_integration.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("external programs must not run"),
    )

    assert (
        sql_integration.run(
            profile="auth",
            include_greenplum=True,
            clickhouse_driver="both",
        )
        == 17
    )
    for driver in ("http", "native"):
        assert (tmp_path / "auth" / driver / "startup.log").read_text() == (
            "service image could not be pulled\n"
        )
    assert len(teardown) == 2
    assert all(command[-3:] == ["down", "--volumes", "--remove-orphans"] for command in teardown)


def test_every_integration_job_reports_captured_startup_errors() -> None:
    workflow = (REPO_ROOT / ".github/workflows/sql-integration.yml").read_text(encoding="utf-8")
    jobs = re.split(r"(?m)^  [a-z][a-z-]*:\n", workflow.split("\njobs:\n", 1)[1])[1:]

    assert len(jobs) == 5
    for job in jobs:
        report = job.split("- name: Report captured startup diagnostics\n", 1)[1]
        report = report.split("      - name:", 1)[0]
        assert "if: failure()" in report
        assert "for log in .integration-artifacts/*/*/startup.log; do" in report
        assert 'tail -n 80 "$log"' in report
        assert "if-no-files-found: error" in job
