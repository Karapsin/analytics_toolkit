from __future__ import annotations

import importlib.util
import json
import subprocess
from typing import TYPE_CHECKING, Any

import pytest

from tests._support.paths import REPO_ROOT

if TYPE_CHECKING:
    from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "github_controller", REPO_ROOT / ".github/agent/controller.py"
)
assert spec is not None
assert spec.loader is not None
controller = importlib.util.module_from_spec(spec)
spec.loader.exec_module(controller)


def checks(base: str = "base", *, integration: str = "failure") -> list[dict[str, Any]]:
    return [
        {"name": "agent-review", "external_id": base, "app": {"id": 17}, "conclusion": "success"},
        {"name": "fast-checks", "app": {"slug": "github-actions"}, "conclusion": "success"},
        {"name": "core SQL integration (HTTP)", "conclusion": integration},
    ]


def test_integration_failure_does_not_gate_dev_merge() -> None:
    assert controller.ready({"mergeable": True, "labels": []}, checks(), "base", 17)


def test_stale_review_wrong_app_and_failed_fast_checks_block_merge() -> None:
    pr = {"mergeable": True, "labels": []}
    assert not controller.ready(pr, checks("old-base"), "base", 17)
    assert not controller.ready(pr, checks(), "base", 99)
    failed = checks()
    failed[1]["conclusion"] = "failure"
    assert not controller.ready(pr, failed, "base", 17)
    assert not controller.ready(
        {"mergeable": True, "labels": [{"name": "agent:writing"}]}, checks(), "base", 17
    )


def test_foreign_fork_is_not_a_trusted_model_job() -> None:
    pr = {
        "draft": False,
        "state": "open",
        "base": {"ref": "dev"},
        "head": {"repo": {"full_name": "someone/fork"}},
    }
    assert not controller.trusted(pr, "owner/repo")


def test_serialized_changelog_fold_threshold_and_version_carry(tmp_path: Path) -> None:
    (tmp_path / "agent_tools/changelog").mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    (tmp_path / "pyproject.toml").write_text('version = "1.3.19.19"\n')
    (tmp_path / "README.md").write_text("**Version:** `1.3.19.19`\n")
    changelog = tmp_path / "docs/CHANGELOG.md"
    changelog.write_text(
        "# Changelog\n\n## Unreleased\n\n"
        + "\n".join("- Existing " + str(i) for i in range(9))
        + "\n\n## 1.3.19.19 - 2026-01-01\n"
    )
    (tmp_path / "agent_tools/changelog/one.md").write_text("- New feature\n")
    assert controller.fold(tmp_path)
    assert 'version = "1.4.0.0"' in (tmp_path / "pyproject.toml").read_text()
    assert "**Version:** `1.4.0.0`" in (tmp_path / "README.md").read_text()
    assert "## 1.4.0.0" in changelog.read_text()
    assert not list((tmp_path / "agent_tools/changelog").glob("*.md"))
    assert not controller.fold(tmp_path)


def test_provisioning_has_no_api_model_execution() -> None:
    text = (REPO_ROOT / ".github/workflows/github-agent.yml").read_text()
    assert "OPENAI_API_KEY" not in text
    assert "codex-action" not in text
    assert "-aes-256-gcm" in text
    assert "github.ref == 'refs/heads/main'" in text
    assert "retention-days: 1" in text


def test_later_failure_is_not_cleared_by_earlier_green() -> None:
    class GitHub:
        def api(self, path: str) -> dict[str, str]:
            return {"status": "ahead"}

    state = {
        "runs": {
            "1": {
                "sha": "a",
                "status": "completed",
                "updated": "2026-10-01",
                "green": ["HTTP"],
                "url": "old",
            },
            "2": {
                "sha": "b",
                "status": "completed",
                "updated": "2026-10-02",
                "needs": ["HTTP", "native"],
            },
        }
    }
    controller._resolve_runs(GitHub(), state)
    assert state["runs"]["2"]["needs"] == ["HTTP", "native"]
    state["runs"]["3"] = {
        "sha": "c",
        "status": "completed",
        "updated": "2026-10-03",
        "green": ["HTTP"],
        "url": "new",
    }
    controller._resolve_runs(GitHub(), state)
    assert state["runs"]["2"]["needs"] == ["native"]


def test_dispatch_bridge_rejects_foreign_senders_and_arbitrary_operations() -> None:
    spec = importlib.util.spec_from_file_location(
        "github_dispatch", REPO_ROOT / ".github/agent/dispatch.py"
    )
    assert spec is not None
    assert spec.loader is not None
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    event = {
        "sender": {"login": "app[bot]", "type": "Bot"},
        "issue": {"title": "GitHub agent integration monitor"},
        "comment": {
            "body": bridge.PREFIX + json.dumps({"operation": "visual", "value": "a" * 40}) + "\n-->"
        },
    }
    assert bridge.validated(event, "app")["value"] == "a" * 40
    event["sender"]["login"] = "someone"
    with pytest.raises(ValueError, match="sender"):
        bridge.validated(event, "app")
    event["sender"]["login"] = "app[bot]"
    event["comment"]["body"] = (
        bridge.PREFIX + json.dumps({"operation": "shell", "value": "x"}) + "\n-->"
    )
    with pytest.raises(ValueError, match="Unsupported"):
        bridge.validated(event, "app")


def test_duplicate_matrix_job_names_require_every_group_to_pass() -> None:
    class GitHub:
        def pages(self, path: str, key: str) -> list[dict[str, str]]:
            return [
                {"name": "fault", "conclusion": result}
                for result in ["success", "failure", "success"]
            ]

    state: dict[str, Any] = {"runs": {}}
    run = {
        "id": 1,
        "status": "completed",
        "conclusion": "failure",
        "run_attempt": 1,
        "head_sha": "a",
        "html_url": "run",
        "event": "schedule",
        "updated_at": "2026-10-01",
    }
    controller._record_run(GitHub(), state, run)
    assert state["runs"]["1"]["needs"] == ["fault"]
    assert state["runs"]["1"]["green"] == []
    controller._resolve_runs(GitHub(), state)
    assert state["runs"]["1"]["needs"] == ["fault"]


def test_rejected_bot_repair_is_queued_for_correction(monkeypatch: Any) -> None:
    base = "b" * 40
    pr = {
        "number": 8,
        "draft": False,
        "state": "open",
        "mergeable": True,
        "labels": [],
        "base": {"ref": "dev"},
        "user": {"type": "Bot", "login": "analytics-toolkit-agent-717959[bot]"},
        "head": {
            "sha": "a" * 40,
            "ref": controller.REPAIR_BRANCH + base[:12],
            "repo": {"full_name": "owner/repo"},
        },
    }

    class GitHub:
        repository = "owner/repo"

        def base(self) -> str:
            return base

        def pulls(self) -> list[dict[str, Any]]:
            return [pr]

        def pull(self, number: int) -> dict[str, Any]:
            return pr

        def pages(self, path: str, key: str) -> list[Any]:
            assert "%2B00%3A00" in path
            return []

        def api(self, path: str) -> dict[str, int]:
            return {"behind_by": 0}

        def checks(self, head: str) -> list[dict[str, Any]]:
            result = checks(base)
            result[0].update(conclusion="failure", status="completed")
            return result

    monkeypatch.setattr(
        controller, "monitor", lambda gh: (99, {"started": "2026-10-01T00:00:00+00:00", "runs": {}})
    )
    monkeypatch.setattr(controller, "save_monitor", lambda *args: None)
    monkeypatch.setattr(controller, "authorization_error", lambda *args: None)
    assert controller.discover(GitHub(), 17)["include"][0]["kind"] == "repair"


def owned_pr() -> dict[str, Any]:
    return {
        "number": 1,
        "draft": False,
        "state": "open",
        "base": {"ref": "dev"},
        "user": {"type": "User", "login": "owner"},
        "head": {"sha": "a" * 40, "ref": "codex/feature", "repo": {"full_name": "owner/repo"}},
    }


def test_only_owner_and_own_repair_bot_are_trusted() -> None:
    pr = owned_pr()
    assert controller.trusted(pr, "owner/repo")
    pr["user"]["login"] = "collaborator"
    assert not controller.trusted(pr, "owner/repo")
    pr["user"] = {"type": "Bot", "login": "foreign[bot]"}
    pr["head"]["ref"] = controller.REPAIR_BRANCH + "base"
    assert not controller.trusted(pr, "owner/repo")
    pr["user"]["login"] = "analytics-toolkit-agent-717959[bot]"
    assert controller.trusted(pr, "owner/repo")


def test_policy_approval_is_owner_only_exact_head_and_covers_renames(monkeypatch: Any) -> None:
    class GitHub:
        repository = "owner/repo"

        def __init__(self) -> None:
            self.comments: list[dict[str, Any]] = []

        def pages(self, path: str) -> list[dict[str, Any]]:
            if "/files?" in path:
                return [{"filename": "notes.md", "previous_filename": "AGENTS.md"}]
            return self.comments

    gh = GitHub()
    pr = owned_pr()
    monkeypatch.setattr(controller, "signed_head", lambda *args: True)
    assert "approval comment" in controller.authorization_error(gh, pr)
    gh.comments = [
        {"user": {"type": "User", "login": "stranger"}, "body": "/agent approve-policy " + "a" * 40}
    ]
    assert controller.authorization_error(gh, pr)
    gh.comments[0]["user"]["login"] = "owner"
    gh.comments[0]["body"] = "/agent approve-policy " + "b" * 40
    assert controller.authorization_error(gh, pr)
    gh.comments[0]["body"] = "/agent approve-policy " + "a" * 40
    assert controller.authorization_error(gh, pr) is None
    monkeypatch.setattr(controller, "signed_head", lambda *args: False)
    assert "enrolled machine" in controller.authorization_error(gh, pr)


def test_unsigned_unknown_and_revoked_machine_keys_fail_closed(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from agent_tools.machine_signing import ensure_key  # noqa: PLC0415

    root = tmp_path / "checkout"
    root.mkdir()

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=root, check=True, capture_output=True, text=True
        ).stdout.strip()

    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
    git("init", "-b", "dev")
    git("config", "user.name", "Device")
    git("config", "user.email", "device@example.test")
    git("remote", "add", "origin", str(origin))
    (root / "source.txt").write_text("original")
    git("add", "source.txt")
    git("commit", "-m", "Unsigned")
    unsigned = git("rev-parse", "HEAD")
    git("push", "origin", "dev")
    key = ensure_key(tmp_path / "device")
    other = ensure_key(tmp_path / "other")
    registry = tmp_path / "allowed_signers"
    registry.write_text("device " + key.with_suffix(".pub").read_text())
    monkeypatch.setenv("AGENT_ALLOWED_SIGNERS", str(registry))
    monkeypatch.setenv("AGENT_CONTROL_DIR", str(root))
    assert not controller.signed_head("owner/repo", unsigned)
    git(
        "-c",
        "gpg.format=ssh",
        "-c",
        "user.signingkey=" + str(key),
        "commit",
        "-S",
        "--allow-empty",
        "-m",
        "Signed by device",
    )
    signed = git("rev-parse", "HEAD")
    git("push", "origin", "dev")
    assert controller.signed_head("owner/repo", signed)
    registry.write_text("other " + other.with_suffix(".pub").read_text())
    assert not controller.signed_head("owner/repo", signed)
    monkeypatch.delenv("AGENT_ALLOWED_SIGNERS")
    assert not controller.signed_head("owner/repo", signed)


def test_candidate_failure_is_assigned_to_squash_merge_for_repair() -> None:
    candidate, merged = "a" * 40, "b" * 40

    class GitHub:
        def pull(self, number: int) -> dict[str, Any]:
            assert number == 7
            return {"merged": True, "merge_commit_sha": merged}

        def pages(self, path: str, key: str) -> list[dict[str, str]]:
            return [{"name": "HTTP", "conclusion": "failure"}]

    state: dict[str, Any] = {"runs": {}}
    controller._record_run(
        GitHub(),
        state,
        {
            "id": 1,
            "head_sha": "c" * 40,
            "display_title": f"agent integration {candidate} PR 7",
            "status": "completed",
            "conclusion": "failure",
            "run_attempt": 1,
            "html_url": "run",
            "event": "workflow_dispatch",
            "updated_at": "2026-10-09",
        },
    )
    assert state["runs"]["1"]["candidate"] == candidate
    assert state["runs"]["1"]["sha"] == merged
    assert state["runs"]["1"]["needs"] == ["HTTP"]


def test_integration_workflow_has_only_explicit_candidate_or_manual_triggers() -> None:
    text = (REPO_ROOT / ".github/workflows/sql-integration.yml").read_text()
    assert "  push:" not in text
    assert "  schedule:" not in text
    assert "inputs.candidate || github.sha" in text
    assert "workflow_dispatch:" in text


def test_conflict_policy_diff_excludes_changes_already_in_dev(tmp_path: Path) -> None:
    def git(*args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()

    git("init")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.test")
    (tmp_path / ".github/agent").mkdir(parents=True)
    (tmp_path / ".github/agent/controller.py").write_text("old policy")
    git("add", ".")
    git("commit", "-m", "Base")
    initial = git("rev-parse", "HEAD")
    (tmp_path / "guide.md").write_text("Feature guide")
    git("add", ".")
    git("commit", "-m", "Feature")
    head = git("rev-parse", "HEAD")
    git("checkout", "--detach", initial)
    (tmp_path / ".github/agent/controller.py").write_text("approved dev policy")
    git("add", ".")
    git("commit", "-m", "Dev policy")
    base = git("rev-parse", "HEAD")
    git("checkout", "--detach", head)
    git("merge", "--no-commit", "--no-ff", base)
    assert ".github/agent/controller.py" in git("diff", "--cached", "--name-only", head)
    assert git("diff", "--cached", "--name-only", base) == "guide.md"


def test_unmerged_candidate_green_does_not_clear_dev_failure() -> None:
    class GitHub:
        def api(self, path: str) -> dict[str, str]:
            return {"status": "ahead"}

    state = {
        "runs": {
            "1": {"sha": "a", "status": "completed", "updated": "2026-10-01", "needs": ["HTTP"]},
            "2": {
                "sha": "b",
                "status": "completed",
                "updated": "2026-10-02",
                "green": ["HTTP"],
                "merged": False,
                "url": "candidate",
            },
        }
    }
    controller._resolve_runs(GitHub(), state)
    assert state["runs"]["1"]["needs"] == ["HTTP"]
    state["runs"]["2"]["merged"] = True
    controller._resolve_runs(GitHub(), state)
    assert state["runs"]["1"]["needs"] == []
