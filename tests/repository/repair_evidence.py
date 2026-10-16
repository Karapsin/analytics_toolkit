from __future__ import annotations

import hashlib
import io
import json
import stat
import zipfile
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest

from tests.repository.github_agent import controller
from tests.repository.review_control import control as control  # noqa: PLC0414 - pytest fixture
from tests.repository.review_control import (
    publication as publication,  # noqa: PLC0414 - expose the shared pytest fixture
)

if TYPE_CHECKING:
    from pathlib import Path

evidence = controller.trusted_module("repair_evidence")


def archive(files: dict[str, str], *, symlink: bool = False) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as output:
        for name, content in files.items():
            info = zipfile.ZipInfo(name)
            if symlink:
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
            output.writestr(info, content)
    return buffer.getvalue()


def test_startup_evidence_is_redacted_hashed_and_bound_to_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh = Mock(repository="owner/repo")
    gh.api.return_value = {"run_attempt": 2, "head_sha": "a" * 40, "run_started_at": "before"}
    gh.pages.return_value = [
        {
            "id": 4,
            "name": "sql-integration-core-http",
            "size_in_bytes": 100,
            "expired": False,
            "created_at": "now",
        }
    ]
    data = archive(
        {
            "startup.log": "pull access denied\nTOKEN=synthetic-private\n",
            "ignored.txt": "not diagnostics",
        }
    )
    monkeypatch.setattr(evidence, "download", lambda repo, identity: data)
    task = {"runs": [7], "evidence_key": evidence.fingerprint(evidence.inventory(gh, [7]))}
    destination = tmp_path / "evidence"
    evidence.prepare(gh, task, destination, controller.trusted_module("task_context").redact)
    manifest = json.loads((destination / "manifest.json").read_text())
    assert manifest["runs"][0]["attempt"] == 2
    assert manifest["runs"][0]["head"] == "a" * 40
    path, digest = next(iter(manifest["files"].items()))
    content = (destination / path).read_bytes()
    assert b"pull access denied" in content
    assert b"synthetic-private" not in content
    assert digest == hashlib.sha256(content).hexdigest()
    assert not list(destination.rglob("ignored.txt"))


@pytest.mark.parametrize("path", ["../startup.log", "/startup.log", "a\\startup.log"])
def test_unsafe_archive_paths_are_rejected(tmp_path: Path, path: str) -> None:
    with pytest.raises(ValueError, match="Unsafe"):
        evidence.extract(archive({path: "bad"}), tmp_path, str)


def test_symlinks_and_large_diagnostics_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unsafe"):
        evidence.extract(archive({"startup.log": "outside"}, symlink=True), tmp_path, str)
    with pytest.raises(ValueError, match="file limit"):
        evidence.extract(archive({"startup.log": "x" * (evidence.MAX_FILE + 1)}), tmp_path, str)


def test_expired_artifact_is_explicit_and_not_downloaded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = [
        {"run": 7, "attempt": 1, "head": "a" * 40, "artifacts": [{"id": 4, "expired": True}]}
    ]
    monkeypatch.setattr(evidence, "inventory", lambda gh, runs: snapshot)
    monkeypatch.setattr(evidence, "download", Mock(side_effect=AssertionError("download")))
    task = {"runs": [7], "evidence_key": evidence.fingerprint(snapshot)}
    evidence.prepare(object(), task, tmp_path / "evidence", str)
    manifest = json.loads((tmp_path / "evidence/manifest.json").read_text())
    assert manifest["missing"] == ["Run 7 attempt 1: artifacts missing or expired"]
    assert manifest["files"] == {}


def test_blocked_repair_retries_only_after_identity_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    snapshot: list[Any] = []
    monkeypatch.setattr(evidence, "inventory", lambda gh, runs: snapshot)
    state = {"runs": {"7": {"sha": "old", "status": "completed", "needs": ["HTTP"]}}}
    task = controller._integration_tasks(object(), state, "a" * 40, [])[0]
    outcome = {"outcome": "blocked", "reason": "missing log", "required_evidence": ["startup.log"]}
    assert evidence.record(task, outcome)
    assert not evidence.record(task, outcome)
    assert controller._integration_tasks(object(), state, "a" * 40, []) == []
    assert state["runs"]["7"]["needs"] == ["HTTP"]
    snapshot.append({"artifact": "new"})
    assert controller._integration_tasks(object(), state, "a" * 40, [])
    assert controller._integration_tasks(object(), state, "b" * 40, [])


@pytest.mark.parametrize("outcome_type", ["blocked", "no_change"])
def test_no_patch_outcome_is_not_published_or_repeated(
    outcome_type: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path / "state"))
    task = {"kind": "repair", "number": 0, "base": "a" * 40, "runs": [7], "evidence_key": "e"}
    task["user_task"] = controller.trusted_module("task_context").repair_context(
        task["base"], "standing-repair:" + task["base"]
    )
    (tmp_path / "task.json").write_text(json.dumps(task))
    (tmp_path / "fix.patch").write_bytes(b"")
    (tmp_path / "verdict.json").write_text(
        json.dumps(
            {
                "outcome": outcome_type,
                "reason": "need diagnostics",
                "required_evidence": ["startup.log"],
            }
        )
    )
    git = Mock()
    monkeypatch.setattr(controller, "command", git)
    gh = Mock(repository="owner/repo")
    gh.base.return_value = task["base"]
    monkeypatch.setattr(controller, "monitor", lambda gh: (1, {"runs": {}}))
    for _ in range(2):
        assert controller.publish(gh, task, tmp_path, tmp_path) == {"status": "blocked"}
    assert gh.comment.call_count == 1
    controls = controller.trusted_module("review_control")
    assert controls.pending() == []
    with controls.connect() as db:
        notifications = db.execute("SELECT key,kind,payload,sent FROM notifications").fetchall()
    assert len(notifications) == 1
    notification = notifications[0]
    assert notification["key"] == "repair-blocked:" + evidence.identity(task)
    assert notification["kind"] == "repair-blocked"
    assert notification["sent"] == -1
    assert "startup.log" in json.loads(notification["payload"])["reason"]
    assert "need diagnostics" in gh.comment.call_args.args[1]
    db = evidence.connection()
    try:
        outcomes = db.execute("SELECT identity,value FROM outcomes").fetchall()
    finally:
        db.close()
    assert outcomes == [
        (
            evidence.identity(task),
            json.dumps(
                {
                    "outcome": outcome_type,
                    "reason": "need diagnostics",
                    "required_evidence": ["startup.log"],
                }
            ),
        )
    ]
    assert evidence.blocked(task)
    assert not evidence.blocked({**task, "evidence_key": "new-evidence"})
    gh.api.assert_not_called()
    git.assert_not_called()


def test_expired_step_logs_do_not_hide_artifact_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = {"kind": "repair", "number": 0, "head": "a" * 40, "base": "a" * 40, "runs": [7]}
    control = tmp_path / "control"
    prompts = control / ".github/agent/prompts"
    prompts.mkdir(parents=True)
    (prompts / "repair.md").write_text("Inspect integration/manifest.json")
    prepared = Mock()
    monkeypatch.setattr(evidence, "prepare", prepared)
    monkeypatch.setattr(
        controller, "command", Mock(side_effect=RuntimeError("gh run failed: HTTP 404"))
    )
    controller.prepare(Mock(repository="owner/repo"), task, tmp_path, control)
    prepared.assert_called_once()
    assert "step logs unavailable" in (control / ".github/agent/task.md").read_text()


@pytest.mark.parametrize("kind", ["conflict", "repair"])
def test_empty_patch_publication_retains_existing_contract(publication: Any, kind: str) -> None:
    work, _git, commit, task, artifact, gh, pushed, _history = publication
    head = commit()
    value = task(head, head, kind)
    output = artifact(value, empty=True)
    if kind == "conflict":
        controller.publish(gh, value, output, work)
        assert len(pushed) == 1
    else:
        (output / "verdict.json").write_text(
            json.dumps({"outcome": "patch", "reason": "claims a patch", "required_evidence": []})
        )
        with pytest.raises(ValueError, match="does not match its patch"):
            controller.publish(gh, value, output, work)
        assert not pushed
