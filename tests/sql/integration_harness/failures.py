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
        assert (tmp_path / "auth" / driver / "startup.log").read_text(encoding="utf-8") == (
            "service image could not be pulled\n"
        )
    assert len(teardown) == 2
    assert all(command[-3:] == ["down", "--volumes", "--remove-orphans"] for command in teardown)


_SERVICE_JOB_IDS = ("core-http", "core-native", "auth", "fault", "stress")


def _workflow_jobs(workflow: str) -> dict[str, str]:
    blocks = re.split(r"(?m)^  ([a-z][a-z0-9_-]*):\n", workflow.split("\njobs:\n", 1)[1])
    return {blocks[index]: blocks[index + 1] for index in range(1, len(blocks), 2)}


def _assert_service_diagnostics(workflow: str) -> None:
    jobs = _workflow_jobs(workflow)
    for name in _SERVICE_JOB_IDS:
        assert name in jobs, f"missing service job: {name}"
        job = jobs[name]
        reports = job.split("- name: Report captured startup diagnostics\n", 1)
        assert len(reports) == 2, name
        report = reports[1].split("      - name:", 1)[0]
        assert "if: failure()" in report, name
        assert "for log in .integration-artifacts/*/*/startup.log; do" in report, name
        assert 'tail -n 80 "$log"' in report, name
        uploads = [
            step for step in job.split("      - name:") if "uses: actions/upload-artifact@" in step
        ]
        profile, _, driver = name.partition("-")
        drivers = (driver,) if driver else ("http", "native")
        for transport in drivers:
            path = f"path: .integration-artifacts/{profile}/{transport}\n"
            assert any(path in upload for upload in uploads), name
        for upload in uploads:
            assert "if: always()" in upload, name
            assert "path: .integration-artifacts/" in upload, name
            assert "if-no-files-found: error" in upload, name


def test_policy_only_jobs_do_not_require_service_diagnostics() -> None:
    workflow = (REPO_ROOT / ".github/workflows/sql-integration.yml").read_text(encoding="utf-8")
    policy = _workflow_jobs(workflow)["policy"]
    assert "Report captured startup diagnostics" not in policy
    workflow += "\n  additional-policy:\n" + policy
    _assert_service_diagnostics(workflow)


def test_reordered_jobs_do_not_change_service_diagnostics() -> None:
    workflow = (REPO_ROOT / ".github/workflows/sql-integration.yml").read_text(encoding="utf-8")
    jobs = _workflow_jobs(workflow)
    reordered = workflow.split("\njobs:\n", 1)[0] + "\njobs:\n"
    reordered += "".join(f"  {name}:\n{body.rstrip()}\n\n" for name, body in reversed(jobs.items()))
    _assert_service_diagnostics(reordered)


@pytest.mark.parametrize("job_id", _SERVICE_JOB_IDS)
def test_missing_service_jobs_are_rejected(job_id: str) -> None:
    workflow = (REPO_ROOT / ".github/workflows/sql-integration.yml").read_text(encoding="utf-8")
    job = _workflow_jobs(workflow)[job_id]
    broken = workflow.replace(f"  {job_id}:\n{job}", "", 1)
    with pytest.raises(AssertionError, match=f"missing service job: {job_id}"):
        _assert_service_diagnostics(broken)


@pytest.mark.parametrize("job_id", _SERVICE_JOB_IDS)
@pytest.mark.parametrize(
    "required",
    [
        "- name: Report captured startup diagnostics",
        "if: failure()",
        "for log in .integration-artifacts/*/*/startup.log; do",
        'tail -n 80 "$log"',
        "if: always()",
        "path: .integration-artifacts/",
        "if-no-files-found: error",
    ],
)
def test_missing_service_diagnostics_are_rejected(job_id: str, required: str) -> None:
    workflow = (REPO_ROOT / ".github/workflows/sql-integration.yml").read_text(encoding="utf-8")
    job = _workflow_jobs(workflow)[job_id]
    broken = job.replace(required, "removed diagnostic requirement", 1)
    assert broken != job
    with pytest.raises(AssertionError, match=job_id):
        _assert_service_diagnostics(workflow.replace(job, broken, 1))


@pytest.mark.parametrize(
    ("job_id", "driver"),
    [
        ("core-http", "http"),
        ("core-native", "native"),
        ("auth", "http"),
        ("auth", "native"),
        ("fault", "http"),
        ("fault", "native"),
        ("stress", "http"),
        ("stress", "native"),
    ],
)
def test_missing_transport_artifacts_are_rejected(job_id: str, driver: str) -> None:
    profile = job_id.partition("-")[0]
    workflow = (REPO_ROOT / ".github/workflows/sql-integration.yml").read_text(encoding="utf-8")
    job = _workflow_jobs(workflow)[job_id]
    steps = job.split("      - name:")
    path = f"path: .integration-artifacts/{profile}/{driver}\n"
    broken = "      - name:".join(step for step in steps if path not in step)
    assert broken != job
    with pytest.raises(AssertionError, match=job_id):
        _assert_service_diagnostics(workflow.replace(job, broken, 1))


def _assert_integration_startup_reports(workflow: str) -> None:
    _assert_service_diagnostics(workflow)
    sections = re.split(
        r"(?m)^  ([a-z][a-z0-9_-]*):\n",
        workflow.split("\njobs:\n", 1)[1],
    )
    jobs = {sections[index]: sections[index + 1] for index in range(1, len(sections), 2)}
    required = {"core-http", "core-native", "auth", "fault", "stress"}
    execution_jobs = {
        name: job
        for name, job in jobs.items()
        if "--level integration" in job or "python -m release_routines.sql_integration" in job
    }

    assert required <= execution_jobs.keys()
    for name, job in execution_jobs.items():
        assert "- name: Report captured startup diagnostics\n" in job, name
        report = job.split("- name: Report captured startup diagnostics\n", 1)[1]
        report = report.split("      - name:", 1)[0]
        assert "if: failure()" in report
        assert "for log in .integration-artifacts/*/*/startup.log; do" in report
        assert 'tail -n 80 "$log"' in report
        assert "if-no-files-found: error" in job


def test_every_integration_job_reports_captured_startup_errors() -> None:
    workflow = (REPO_ROOT / ".github/workflows/sql-integration.yml").read_text(encoding="utf-8")
    _assert_integration_startup_reports(workflow)


def test_startup_report_guard_accepts_separate_policy_job() -> None:
    workflow = (REPO_ROOT / ".github/workflows/sql-integration.yml").read_text(encoding="utf-8")
    policy = (
        "  candidate-policy:\n"
        "    runs-on: ubuntu-latest\n"
        "    outputs:\n"
        "      core: ${{ steps.policy.outputs.core }}\n"
        "    steps:\n"
        "      - run: echo policy\n"
    )
    workflow = workflow.replace("\njobs:\n", "\njobs:\n" + policy, 1)

    _assert_integration_startup_reports(workflow)


@pytest.mark.parametrize("job_name", ["core-http", "core-native", "auth", "fault", "stress"])
def test_startup_report_guard_still_rejects_missing_execution_diagnostics(job_name: str) -> None:
    workflow = (REPO_ROOT / ".github/workflows/sql-integration.yml").read_text(encoding="utf-8")
    prefix, job = workflow.split(f"  {job_name}:\n", 1)
    job = job.replace(
        "- name: Report captured startup diagnostics\n",
        "- name: Missing startup diagnostics\n",
        1,
    )

    with pytest.raises(AssertionError, match=job_name):
        _assert_integration_startup_reports(prefix + f"  {job_name}:\n" + job)


def test_startup_report_guard_checks_additional_execution_jobs() -> None:
    workflow = (REPO_ROOT / ".github/workflows/sql-integration.yml").read_text(encoding="utf-8")
    workflow += (
        "\n  extra-suite:\n"
        "    steps:\n"
        "      - run: python -m release_routines.sql_integration --profile core\n"
    )

    with pytest.raises(AssertionError, match="extra-suite"):
        _assert_integration_startup_reports(workflow)


@pytest.mark.parametrize("cleanup_phase", ["down", "leaks"])
@pytest.mark.parametrize(
    "cleanup_case",
    [(17, "native", 17), (0, "http", 23), (0, "native", 23)],
)
def test_cleanup_failure_keeps_first_status_and_attempts_both_transports(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    cleanup_phase: str,
    cleanup_case: tuple[int, str, int],
) -> None:
    http_result, cleanup_driver, expected = cleanup_case
    starts: list[str] = []
    teardown: list[str] = []
    leak_checks: list[str] = []

    def capture(path: Path, _command) -> int:
        driver = path.parent.name
        starts.append(driver)
        path.write_text("stack ready\n", encoding="utf-8")
        return 0

    def run_command(command, *, env=None) -> int:
        if command[-3:] == ["down", "--volumes", "--remove-orphans"]:
            project = command[command.index("--project-name") + 1]
            driver = project.rsplit("-", 1)[1]
            teardown.append(driver)
            return 23 if cleanup_phase == "down" and driver == cleanup_driver else 0
        assert env is not None
        driver = env["SQL_INTEGRATION_CLICKHOUSE_DRIVER"]
        return http_result if driver == "http" else 0

    def check_leaks(*, profile: str, clickhouse_driver: str) -> int:
        assert profile == "core"
        leak_checks.append(clickhouse_driver)
        return 23 if cleanup_phase == "leaks" and clickhouse_driver == cleanup_driver else 0

    monkeypatch.setattr(sql_integration, "ARTIFACTS_DIR", tmp_path)
    monkeypatch.setattr(sql_integration.importlib.util, "find_spec", lambda _name: object())
    monkeypatch.setattr(sql_integration, "_capture", capture)
    monkeypatch.setattr(sql_integration, "_run", run_command)
    monkeypatch.setattr(sql_integration, "_write_diagnostics", lambda **_kwargs: None)
    monkeypatch.setattr(sql_integration, "_assert_no_manifest_skips", lambda **_kwargs: 0)
    monkeypatch.setattr(sql_integration, "_assert_teardown_clean", check_leaks)
    monkeypatch.setattr(
        sql_integration,
        "_assert_transport_scenario_parity",
        lambda **_kwargs: pytest.fail("cleanup failures must not report collection parity"),
    )
    monkeypatch.setattr(
        sql_integration.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("external programs must not run"),
    )

    assert (
        sql_integration.run(
            profile="all",
            include_greenplum=True,
            clickhouse_driver="both",
        )
        == expected
    )
    assert starts == teardown == leak_checks == ["http", "native"]
    for driver in starts:
        assert (tmp_path / "core" / driver / "startup.log").read_text(encoding="utf-8") == (
            "stack ready\n"
        )
