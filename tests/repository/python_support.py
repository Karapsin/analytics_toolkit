from __future__ import annotations

import base64
import importlib.util
import io
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import TYPE_CHECKING

from release_routines.lib import python_support as policy

from tests._support.paths import REPO_ROOT

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

spec = importlib.util.spec_from_file_location(
    "python_support_controller", REPO_ROOT / ".github/agent/python_support.py"
)
assert spec is not None
assert spec.loader is not None
maintenance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(maintenance)


def fixture_policy() -> dict:
    return {
        "normal_window": 7,
        "minimum": "3.9",
        "maximum": "3.15",
        "extras": {
            extra: {"minimum": "3.10" if extra == "datalens" else "3.9", "maximum": "3.14"}
            for extra in policy.EXTRAS
        },
    }


def test_readiness_is_bound_to_exact_base_minor_and_core_success() -> None:
    now = datetime.now(timezone.utc)
    run = {
        "id": 1,
        "display_title": "Python support " + "a" * 40 + " 3.15",
        "head_branch": "main",
        "path": maintenance.WORKFLOW,
        "status": "completed",
        "created_at": now.isoformat(),
        "html_url": "evidence",
    }
    jobs = [
        {"name": f"probe ({extra})", "conclusion": "success"}
        for extra in (*policy.EXTRAS, "all", "core")
    ]
    gh = SimpleNamespace(pages=lambda _path, key: [run] if key == "workflow_runs" else jobs)
    assert maintenance.ready_extras(gh, "a" * 40, "3.15", now) == (
        {*policy.EXTRAS, "all"},
        "evidence",
    )
    assert maintenance.ready_extras(gh, "b" * 40, "3.15", now) == (set(), None)
    assert maintenance.ready_extras(gh, "a" * 40, "3.16", now) == (set(), None)
    jobs[-1]["conclusion"] = "failure"
    assert maintenance.ready_extras(gh, "a" * 40, "3.15", now) == (set(), "evidence")
    run["created_at"] = (now - timedelta(days=3)).isoformat()
    assert maintenance.ready_extras(gh, "a" * 40, "3.15", now) == (set(), None)


def test_daily_discovery_preserves_policy_on_failure_and_reports_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AGENT_CONTROL_DIR", str(tmp_path))
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    current = fixture_policy()
    gh = SimpleNamespace(
        api=lambda _path: {
            "type": "file",
            "encoding": "base64",
            "content": base64.b64encode(json.dumps(current).encode()).decode(),
        }
    )
    calls = []

    def unavailable(*_args: object, **_kwargs: object) -> None:
        calls.append("feed")
        message = "release feed unavailable"
        raise OSError(message)

    monkeypatch.setattr(maintenance, "urlopen", unavailable)
    state = {}
    maintenance.tick(SimpleNamespace(), gh, state, "a" * 40, [])
    assert state["python_support"]["error"] == "release feed unavailable"
    assert current["maximum"] == "3.15"
    maintenance.tick(SimpleNamespace(), gh, state, "a" * 40, [])
    assert calls == ["feed"]


def test_discovery_proposes_overlap_and_never_tests_on_host(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AGENT_CONTROL_DIR", str(tmp_path))
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    current = fixture_policy()
    current["maximum"] = "3.14"
    monkeypatch.setattr(maintenance, "read_policy", lambda *_args: current)
    feed = json.dumps(
        [{"name": "Python 3.15.0", "is_published": True, "pre_release": False}]
    ).encode()
    monkeypatch.setattr(maintenance, "urlopen", lambda *_args, **_kwargs: io.BytesIO(feed))
    monkeypatch.setattr(maintenance, "ready_extras", lambda *_args: (set(), None))
    proposed = []
    monkeypatch.setattr(
        maintenance, "publish_proposal", lambda *_args: proposed.append(_args[4]) or "pr-url"
    )
    dispatched = []
    controller = SimpleNamespace(dispatch=lambda *_args: dispatched.append(_args[1:]))
    state = {}
    maintenance.tick(controller, SimpleNamespace(), state, "a" * 40, [])
    assert proposed[0]["minimum"] == "3.9"
    assert proposed[0]["maximum"] == "3.15"
    assert proposed[0]["extras"]["airflow"]["maximum"] == "3.14"
    assert dispatched == [("python-support", {"candidate": "a" * 40, "python": "3.15"})]
    assert state["python_support"]["proposal"] == "pr-url"


def test_closed_proposals_are_not_reopened_or_force_pushed() -> None:
    current = fixture_policy()
    proposed = policy.transition(current, "3.15", set())
    gh = SimpleNamespace(repository="owner/repo", pages=lambda _path: [{"html_url": "closed-pr"}])
    assert (
        maintenance.publish_proposal(
            SimpleNamespace(), gh, "a" * 40, current, proposed, "3.15", "pending"
        )
        == "closed-pr"
    )


def test_probe_workflow_never_exposes_write_credentials() -> None:
    workflow = (REPO_ROOT / maintenance.WORKFLOW).read_text()
    assert "contents: read" in workflow
    assert "persist-credentials: false" in workflow
    assert "ref: ${{ inputs.candidate }}" in workflow
    assert "needs: prepare" in workflow
    assert "extra: [core, airflow, clickhouse-native, tui, datalens, all]" in workflow
    assert "AGENT_APP_PRIVATE_KEY" not in workflow


def test_trusted_generator_and_parser_helpers_keep_policy_approval() -> None:
    controller_spec = importlib.util.spec_from_file_location(
        "python_policy_guard",
        REPO_ROOT / ".github/agent/controller.py",
    )
    assert controller_spec is not None
    assert controller_spec.loader is not None
    controller = importlib.util.module_from_spec(controller_spec)
    controller_spec.loader.exec_module(controller)
    for path in (
        "release_routines/lib/python_support.py",
        "release_routines/lib/project_metadata.py",
        "release_routines/lib/check_readme_dependencies.py",
        ".github/workflows/python-support.yml",
    ):
        assert controller.policy_path(path)
    assert not controller.policy_path("analytics_toolkit/sql/connection/airflow_compat.py")
