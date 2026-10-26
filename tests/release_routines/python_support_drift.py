from __future__ import annotations

import json
import subprocess
from datetime import date
from typing import TYPE_CHECKING, Any

import pytest
from release_routines.lib import python_support, python_support_drift
from release_routines.lib.python_support_drift import ReleaseDataUnavailable

from tests._support.paths import REPO_ROOT

if TYPE_CHECKING:
    from pathlib import Path

TODAY = date(2026, 10, 26)
EXTRAS = python_support.EXTRAS


def _policy(**changes: Any) -> dict[str, Any]:
    policy = json.loads((REPO_ROOT / python_support.POLICY_PATH).read_text(encoding="utf-8"))
    policy.update(changes)
    python_support.validate_policy(policy)
    return dict(policy)


def _probe(python: str, *, core: bool = True, lagging: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "python": python,
        "core": core,
        "extras": {extra: extra not in lagging for extra in EXTRAS},
    }


def _released(newest: int) -> list[str]:
    return [f"3.{value}" for value in range(newest + 1)]


def test_current_window_is_not_stale() -> None:
    report = python_support_drift.evaluate_drift(_policy(), _released(14), _probe("3.14"))
    assert not report.stale
    assert report.expected == {
        "newest": "3.14",
        "minimum": "3.8",
        "lagging_extras": [],
        "window": 7,
        "requires_python": ">=3.8,<3.15",
    }
    assert "is current: 3.8-3.14" in report.message(_policy())


def test_new_minor_with_supported_extras_moves_window_forward() -> None:
    report = python_support_drift.evaluate_drift(_policy(), _released(15), _probe("3.15"))
    message = report.message(_policy())
    assert report.stale
    assert "STALE PYTHON SUPPORT WINDOW" in message
    assert "CPython 3.15 has been released, but the policy window still ends at 3.14" in message
    assert "newest=3.15, minimum=3.9, lagging_extras=[]" in message
    assert "requires-python >=3.9,<3.16" in message


def test_new_minor_with_lagging_extra_extends_window_to_eight() -> None:
    report = python_support_drift.evaluate_drift(
        _policy(), _released(15), _probe("3.15", lagging=("airflow",))
    )
    assert report.expected is not None
    assert report.expected["minimum"] == "3.8"
    assert report.expected["window"] == 8
    assert report.expected["lagging_extras"] == ["airflow"]


def test_lagging_extra_catching_up_requests_shrink_back_to_seven() -> None:
    policy = _policy(newest="3.15", minimum="3.8", lagging_extras=["airflow", "tui"])
    partial = python_support_drift.evaluate_drift(
        policy, _released(15), _probe("3.15", lagging=("tui",))
    )
    assert partial.reasons == [
        "Lagging extras now install on CPython 3.15: airflow; update lagging."
    ]
    complete = python_support_drift.evaluate_drift(policy, _released(15), _probe("3.15"))
    assert "shrink the window back to seven minors" in complete.reasons[0]
    assert complete.expected is not None
    assert complete.expected["minimum"] == "3.9"
    unchanged = python_support_drift.evaluate_drift(
        policy, _released(15), _probe("3.15", lagging=("airflow", "tui"))
    )
    assert not unchanged.stale


def test_extra_losing_newest_support_requests_extension() -> None:
    report = python_support_drift.evaluate_drift(
        _policy(), _released(14), _probe("3.14", lagging=("datalens",))
    )
    assert "Extras no longer install on CPython 3.14: datalens" in report.reasons[0]
    assert report.expected is not None
    assert report.expected["minimum"] == "3.7"


def test_policy_ahead_of_releases_and_core_failure_are_stale() -> None:
    policy = _policy(newest="3.16", minimum="3.10", runtime_floor="3.10")
    report = python_support_drift.evaluate_drift(policy, _released(15), _probe("3.15", core=False))
    assert report.reasons == [
        "The policy window ends at 3.16, but the newest stable CPython is 3.15.",
        "Core dependencies do not resolve on CPython 3.15.",
    ]
    assert report.expected is None
    assert "cannot be computed" in report.message(policy)


@pytest.mark.parametrize(
    ("released", "probe"),
    [
        ([], _probe("3.14")),
        (_released(15), _probe("3.14")),
        (_released(14), {"python": "3.14", "core": True, "extras": {}}),
    ],
)
def test_mismatched_or_missing_facts_are_unavailable(
    released: list[str], probe: dict[str, Any]
) -> None:
    with pytest.raises(ReleaseDataUnavailable):
        python_support_drift.evaluate_drift(_policy(), released, probe)


def test_endoflife_and_python_org_payloads_yield_stable_minors() -> None:
    payload = {
        "result": {
            "releases": [
                {"name": "3.16", "releaseDate": "2027-10-01", "latest": {"name": "3.16.0"}},
                {"name": "3.15", "releaseDate": "2026-10-09", "latest": {"name": "3.15.1"}},
                {"name": "3.14", "releaseDate": "2025-10-07", "latest": {"name": "3.14.9"}},
                {"name": "3.17", "releaseDate": None, "latest": None},
                {"name": "2.7", "releaseDate": "2010-07-03", "latest": {"name": "2.7.18"}},
            ]
        }
    }
    assert python_support_drift.parse_endoflife(payload, TODAY) == ["3.14", "3.15"]
    releases = [
        {"name": "Python 3.15.0", "is_published": True, "pre_release": False},
        {"name": "Python 3.16.0a1", "is_published": True, "pre_release": True},
        {"name": "Python 3.14.9", "is_published": True, "pre_release": False},
        {"name": "Python 3.17.0", "is_published": False, "pre_release": False},
    ]
    assert python_support_drift.parse_python_org(releases) == ["3.14", "3.15"]


@pytest.mark.parametrize(
    "payload",
    [{}, {"result": {"releases": [{"name": "3.15", "releaseDate": "bad"}]}}, {"result": []}],
)
def test_malformed_endoflife_payloads_are_unavailable(payload: Any) -> None:
    with pytest.raises(ReleaseDataUnavailable):
        python_support_drift.parse_endoflife(payload, TODAY)


def test_python_org_rejects_non_list_and_empty_payloads() -> None:
    with pytest.raises(ReleaseDataUnavailable, match="malformed"):
        python_support_drift.parse_python_org({})
    with pytest.raises(ReleaseDataUnavailable, match="no stable"):
        python_support_drift.parse_python_org([])


def test_release_fetch_falls_back_and_reports_every_source_failure() -> None:
    def python_org_only(url: str) -> Any:
        if url == python_support_drift.ENDOFLIFE_URL:
            message = "offline"
            raise OSError(message)
        return [{"name": "Python 3.15.0", "is_published": True, "pre_release": False}]

    assert python_support_drift.fetch_released_minors(TODAY, python_org_only) == ["3.15"]

    def offline(url: str) -> Any:
        message = f"offline {url}"
        raise OSError(message)

    with pytest.raises(ReleaseDataUnavailable, match="every source") as raised:
        python_support_drift.fetch_released_minors(TODAY, offline)
    assert "endoflife.date" in str(raised.value)
    assert "python.org" in str(raised.value)


@pytest.mark.parametrize(
    ("returncode", "output", "outcome"),
    [
        (0, "", "supported"),
        (1, "ERROR: ResolutionImpossible: requires a different Python", "unsupported"),
        (1, "error: metadata-generation-failed", "unsupported"),
        (1, "Max retries exceeded with url: /simple/pandas/", "unavailable"),
        (1, "HTTP error 503 Service Unavailable", "unavailable"),
        (124, "pip installation timed out after 1200 seconds", "unavailable"),
        (1, "ERROR: Failed building wheel for numpy", "unsupported"),
    ],
)
def test_pip_outcomes_distinguish_unsupported_from_unavailable(
    returncode: int, output: str, outcome: str
) -> None:
    assert python_support_drift.classify_probe(returncode, output) == outcome


def test_probe_resolves_core_and_each_extra_with_core(capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[list[str]] = []

    def run(requirements: list[str]) -> tuple[int, str]:
        calls.append(requirements)
        if "tui-dep" in requirements:
            return 1, "ResolutionImpossible"
        return 0, ""

    requirements = {
        "core": ["core-dep"],
        **{extra: ["core-dep", f"{extra}-dep"] for extra in EXTRAS},
    }
    result = python_support_drift.probe_support("3.15", requirements, run)
    assert result == _probe("3.15", lagging=("tui",))
    assert calls[0] == [python_support_drift.CONTROL_REQUIREMENT]
    assert "tui does not resolve on CPython 3.15" in capsys.readouterr().out


@pytest.mark.parametrize("failing", ["control", "airflow-dep"])
def test_probe_network_failures_are_unavailable(failing: str) -> None:
    def run(requirements: list[str]) -> tuple[int, str]:
        if failing == "control" and requirements == [python_support_drift.CONTROL_REQUIREMENT]:
            return 1, "anything"
        if failing in requirements:
            return 1, "ConnectionError: connection reset"
        return 0, ""

    requirements = {"core": [], **{extra: [f"{extra}-dep"] for extra in EXTRAS}}
    with pytest.raises(ReleaseDataUnavailable):
        python_support_drift.probe_support("3.14", requirements, run)


def test_requirement_sets_join_core_with_each_extra() -> None:
    sets = python_support_drift.requirement_sets(REPO_ROOT)
    assert set(sets) == {"core", *EXTRAS}
    assert any(item.startswith("apache-airflow") for item in sets["airflow"])
    assert set(sets["core"]) <= set(sets["tui"])


def test_cli_exit_codes_distinguish_stale_from_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    released = tmp_path / "released.json"
    probe = tmp_path / "probe.json"
    outputs = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(outputs))
    monkeypatch.setattr(
        python_support_drift, "fetch_released_minors", lambda today: ["3.13", "3.14"]
    )
    assert python_support_drift.main(["released", "--output", str(released)]) == 0
    assert outputs.read_text() == "newest=3.14\n"
    probe.write_text(json.dumps(_probe("3.14")))
    arguments = ["evaluate", "--root", str(REPO_ROOT), "--released", str(released)]
    assert python_support_drift.main([*arguments, "--probe", str(probe)]) == 0
    assert "is current" in capsys.readouterr().out

    released.write_text(json.dumps({"minors": _released(15)}))
    probe.write_text(json.dumps(_probe("3.15")))
    assert (
        python_support_drift.main([*arguments, "--probe", str(probe)]) == python_support_drift.STALE
    )
    assert "::error title=Stale Python support window::" in capsys.readouterr().err

    probe.write_text(json.dumps(_probe("3.14")))
    assert (
        python_support_drift.main([*arguments, "--probe", str(probe)])
        == python_support_drift.UNAVAILABLE
    )
    assert "RELEASE DATA UNAVAILABLE" in capsys.readouterr().err

    def unavailable(today: date) -> list[str]:
        message = "offline"
        raise ReleaseDataUnavailable(message)

    monkeypatch.setattr(python_support_drift, "fetch_released_minors", unavailable)
    assert (
        python_support_drift.main(["released", "--output", str(released)])
        == python_support_drift.UNAVAILABLE
    )


def test_cli_probe_writes_running_interpreter_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "probe.json"
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.setattr(
        python_support_drift,
        "probe_support",
        lambda python, requirements: _probe(python),
    )
    assert (
        python_support_drift.main(["probe", "--root", str(REPO_ROOT), "--output", str(output)]) == 0
    )
    assert json.loads(output.read_text())["core"] is True


def test_probe_installs_into_fresh_environment_so_unbuildable_sdists_are_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[Path] = []
    commands: list[list[str]] = []

    def create(self: Any, directory: Path) -> None:
        created.append(directory)

    def run(command: list[str], **kwargs: Any) -> Any:
        commands.append(command)
        assert kwargs["timeout"] == python_support_drift.PROBE_TIMEOUT_SECONDS
        return subprocess.CompletedProcess(command, 1, "", "error: Failed building wheel for x")

    monkeypatch.setattr(python_support_drift.venv.EnvBuilder, "create", create)
    monkeypatch.setattr(python_support_drift.subprocess, "run", run)
    returncode, output = python_support_drift._run_pip(["native-sdist==1.0"])
    assert python_support_drift.classify_probe(returncode, output) == "unsupported"
    assert commands[0][0] == str(python_support_drift._environment_python(created[0]))
    assert commands[0][1:4] == ["-m", "pip", "install"]
    assert "--dry-run" not in commands[0]
    assert commands[0][-1] == "native-sdist==1.0"
    assert not created[0].parent.exists()

    def timeout(command: list[str], **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(command, 5)

    monkeypatch.setattr(python_support_drift.subprocess, "run", timeout)
    returncode, output = python_support_drift._run_pip(["slow"])
    assert python_support_drift.classify_probe(returncode, output) == "unavailable"


def test_probe_environment_creation_failure_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(self: Any, directory: Path) -> None:
        raise subprocess.CalledProcessError(1, ["python", "-m", "ensurepip"])

    monkeypatch.setattr(python_support_drift.venv.EnvBuilder, "create", broken)
    with pytest.raises(ReleaseDataUnavailable, match="disposable probe environment"):
        python_support_drift._run_pip(["anything"])
