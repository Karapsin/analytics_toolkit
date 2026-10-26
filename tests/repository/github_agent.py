from __future__ import annotations

import base64
import importlib.util
import json
import subprocess
from datetime import datetime, timezone
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


def test_integration_failure_does_not_gate_dev_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    pr = owned_pr()
    pr.update(mergeable=True, labels=[])
    assert controller.ready(pr, checks(), "base", 17)


@pytest.mark.parametrize("condition", ["stale-base", "wrong-app", "failed-fast-check", "writing"])
def test_stale_review_wrong_app_and_failed_fast_checks_block_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, condition: str
) -> None:
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    pr = owned_pr()
    pr.update(mergeable=True, labels=[])
    successful_checks = checks()
    assert controller.ready(pr, successful_checks, "base", 17)

    if condition == "stale-base":
        successful_checks[0]["external_id"] = "old-base"
    elif condition == "wrong-app":
        successful_checks[0]["app"]["id"] = 99
    elif condition == "failed-fast-check":
        successful_checks[1]["conclusion"] = "failure"
    else:
        pr["labels"] = [{"name": "agent:writing"}]

    assert not controller.ready(pr, successful_checks, "base", 17)


def test_foreign_fork_is_not_a_trusted_model_job() -> None:
    pr = {
        "draft": False,
        "state": "open",
        "base": {"ref": "dev", "repo": {"full_name": "owner/repo"}},
        "head": {"repo": {"full_name": "someone/fork"}},
    }
    assert not controller.trusted(pr, "owner/repo")


@pytest.mark.parametrize("count", [8, 9, 10, 20])
def test_serialized_changelog_fold_preserves_versions_and_wrapped_notes(
    tmp_path: Path,
    count: int,
) -> None:
    (tmp_path / "agent_tools/changelog").mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    (tmp_path / "pyproject.toml").write_text('version = "1.3.19.19"\n', encoding="utf-8")
    (tmp_path / "README.md").write_text("**Version:** `1.3.19.19`\n", encoding="utf-8")
    metadata_paths = [tmp_path / "pyproject.toml", tmp_path / "README.md"]
    original_metadata = [path.read_bytes() for path in metadata_paths]
    pending_notes = (
        "\n".join("- Existing " + str(i) for i in range(count)) + "\n  wrapped continuation"
    )
    historical_section = (
        "## 1.3.19.19 - 2026-01-01\n\n- Released feature\n  historical continuation\n"
    )
    changelog = tmp_path / "docs/CHANGELOG.md"
    changelog.write_text(
        "# Changelog\n\n## Unreleased\n\n" + pending_notes + "\n\n" + historical_section,
        encoding="utf-8",
    )
    (tmp_path / "agent_tools/changelog/one.md").write_text("- New feature\n", encoding="utf-8")
    assert controller.fold(tmp_path)
    assert [path.read_bytes() for path in metadata_paths] == original_metadata
    assert changelog.read_text(encoding="utf-8") == (
        "# Changelog\n\n## Unreleased\n\n"
        + pending_notes
        + "\n- New feature\n\n"
        + historical_section
    )
    assert not list((tmp_path / "agent_tools/changelog").glob("*.md"))
    folded_changelog = changelog.read_bytes()
    assert not controller.fold(tmp_path)
    assert changelog.read_bytes() == folded_changelog
    assert [path.read_bytes() for path in metadata_paths] == original_metadata
    assert folded_changelog.count(b"- New feature\n") == 1


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


def owned_pr() -> dict[str, Any]:
    return {
        "number": 1,
        "draft": False,
        "state": "open",
        "base": {"ref": "dev", "repo": {"full_name": "owner/repo"}},
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


@pytest.mark.parametrize(
    ("filename", "previous"),
    [
        ("notes.md", ".github/agent/controller.py"),
        ("notes.md", "agent_tools/sql_explorer_visual_render.py"),
        ("agent_tools/sql_explorer_visual_render.py", "notes.md"),
        ("agent_tools/sql_explorer_visual_render.py", ""),
    ],
)
def test_policy_approval_is_owner_only_exact_head_and_covers_renames(
    monkeypatch: Any, filename: str, previous: str
) -> None:
    class GitHub:
        repository = "owner/repo"

        def __init__(self) -> None:
            self.comments: list[dict[str, Any]] = []

        def pages(self, path: str) -> list[dict[str, Any]]:
            if "/files?" in path:
                return [{"filename": filename, "previous_filename": previous}]
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


def test_post_merge_failure_creates_repair_without_new_dev_matrix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        controller.trusted_module("repair_evidence"), "inventory", lambda gh, runs: []
    )
    state = {
        "runs": {
            "1": {"sha": "old", "status": "completed", "needs": ["HTTP"], "merged": True},
        }
    }
    result = controller._integration_tasks(object(), state, "current", [])
    assert len(result) == 1
    assert {
        key: result[0][key] for key in ("kind", "number", "head", "base", "branch", "runs")
    } == {
        "kind": "repair",
        "number": 0,
        "head": "current",
        "base": "current",
        "branch": controller.REPAIR_BRANCH + "current",
        "runs": [1],
    }
    assert result[0]["producer_id"] == "standing-repair:current"
    proto = controller.trusted_module("task_context")
    assert result[0]["task_digest"] == proto.digest(result[0]["user_task"])
    assert controller._integration_tasks(object(), state, "current", []) == result


class _RefreshDiscoveryGitHub:
    repository = "owner/repo"

    def __init__(self, base: str) -> None:
        self._base = base
        self.errors: dict[str, Exception] = {}
        self.calls: list[str] = []
        self.run_metadata: dict[str, Any] = {
            str(key): {
                "id": key,
                "head_sha": base,
                "status": "in_progress",
                "conclusion": None,
                "run_attempt": 1,
                "html_url": f"run-{key}",
                "event": "workflow_dispatch",
                # Recent enough to survive the monitor's 14-day retention cutoff.
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            for key in (1, 2, 3)
        }
        self.prs = [dict(owned_pr(), number=number) for number in (1, 2)]

    def base(self) -> str:
        return self._base

    def pulls(self) -> list[dict[str, Any]]:
        return self.prs

    def pull(self, number: int) -> dict[str, Any]:
        return self.prs[number - 1]

    def checks(self, head: str) -> list[Any]:
        return []

    def api(self, path: str, method: str = "GET", data: Any = None) -> Any:
        self.calls.append(path)
        if path in self.errors:
            raise self.errors[path]
        route = path.split("?", 1)[0]
        if route.startswith("issues/") and route.endswith("/labels"):
            pr = self.pull(int(route.split("/")[1]))
            if method == "PUT":
                pr["labels"] = [{"name": name} for name in data["labels"]]
            return pr.get("labels", [])
        return self._response(path, method)

    def _response(self, path: str, method: str) -> Any:
        if path.startswith("contents/agent_tools/changelog/"):
            return {"content": base64.b64encode(b"- Valid task fragment\n").decode()}
        if path.startswith("actions/runs/"):
            return self.run_metadata[path.rsplit("/", 1)[1]]
        if path == "collaborators/owner/permission":
            return {"permission": "write"}
        if path.startswith("compare/"):
            return {"behind_by": 0, "status": "identical"}
        if path.startswith("issues/") and path.endswith("/labels"):
            return self.pull(int(path.split("/")[1])).get("labels", [])
        return self._patch_monitor(path, method)

    def _patch_monitor(self, path: str, method: str) -> None:
        if path == "check-runs":
            # Receipt-less owner PRs are marked action_required, never queued.
            assert method == "POST"
            return
        assert path == "issues/99"
        assert method == "PATCH"

    def pages(self, path: str, key: str | None = None) -> list[Any]:
        if path in self.errors:
            raise self.errors[path]
        if key == "workflow_runs":
            return []
        if key == "jobs":
            return [{"name": "HTTP", "conclusion": "success"}]
        assert "/files?" in path
        return (
            [{"filename": "agent_tools/changelog/request.md"}]
            if (path.startswith("pulls/1/"))
            else [{"filename": "source.py"}]
        )


@pytest.fixture
def run_refresh_discovery(monkeypatch: Any) -> tuple[Any, dict[str, Any], list[Any], Any]:
    """Bound discovery to fake GitHub metadata and in-memory monitor persistence."""
    base = "b" * 40
    state: dict[str, Any] = {
        "issue": 99,
        "started": "2026-10-01T00:00:00+00:00",
        "runs": {
            "1": {"sha": base, "status": "in_progress", "needs": [], "attempt": 1},
            "2": {
                "sha": base,
                "status": "completed",
                "conclusion": "failure",
                "needs": ["HTTP"],
                "attempt": 1,
                "updated": "2026-10-01",
            },
            "3": {"sha": base, "status": "queued", "attempt": 1},
        },
    }
    saved: list[Any] = []
    proto = controller.trusted_module("task_context")
    context = {
        "version": 1,
        "task_id": "refresh-test",
        "source": "user",
        "producer_id": "local:refresh-test",
        "starting_prompt": "Fix metadata refresh.",
        "corrections": [],
    }
    metadata = {
        "user_task": context,
        "task_digest": proto.digest(context),
        "producer_id": context["producer_id"],
    }

    original_save = controller.save_monitor

    def save(gh: Any, value: dict[str, Any]) -> None:
        original_save(gh, value)
        saved.append(json.loads(json.dumps(value)))

    monkeypatch.delenv("AGENT_STATE_DIR", raising=False)
    monkeypatch.setattr(controller, "monitor", lambda gh: (99, state))
    monkeypatch.setattr(controller, "save_monitor", save)
    monkeypatch.setattr(controller, "signed_head", lambda *args: True)
    monkeypatch.setattr(controller, "task_metadata", lambda *args: metadata)
    controls = controller.trusted_module("review_control")
    monkeypatch.setattr(controls, "paused", lambda *args: False)
    monkeypatch.setattr(controls, "correction", lambda *args: None)
    return _RefreshDiscoveryGitHub(base), state, saved, metadata


@pytest.mark.parametrize("status", [500, 502, 503, 599])
def test_run_metadata_5xx_preserves_records_and_pr_work_then_retries(
    run_refresh_discovery: Any, status: int
) -> None:
    gh, state, saved, metadata = run_refresh_discovery
    before = json.loads(json.dumps(state["runs"]))
    error = RuntimeError(f"gh api failed: gh: Server Error (HTTP {status})")
    gh.errors = {"actions/runs/1": error, "actions/runs/2": error}

    result = controller.discover(gh, 17)
    assert [(task["number"], task["kind"]) for task in result["include"]] == [(1, "metadata")]
    for task in result["include"]:
        assert {key: task[key] for key in metadata} == metadata
    for key in ("1", "2"):
        assert saved[-1]["runs"][key] == before[key]
    assert saved[-1]["runs"]["3"]["status"] == "in_progress"
    assert gh.calls.count("actions/runs/3") == 1

    gh.errors.clear()
    for key in ("1", "2"):
        gh.run_metadata[key].update(status="completed", conclusion="success", run_attempt=2)
    controller.discover(gh, 17)
    for key in ("1", "2"):
        assert gh.calls.count(f"actions/runs/{key}") == 2
        assert saved[-1]["runs"][key]["status"] == "completed"
        assert saved[-1]["runs"][key]["attempt"] == 2
        assert saved[-1]["runs"][key]["needs"] == []


@pytest.mark.parametrize(
    "message",
    [
        "gh api failed: gh: Unauthorized (HTTP 401)",
        "gh api failed: gh: Forbidden (HTTP 403)",
        "gh api failed: gh: Not Found (HTTP 404)",
        "gh api failed: gh: Rate limit (HTTP 429)",
        "gh api failed: gh: Server Error",
        "gh api failed: gh: Server Error (HTTP 50)",
        "gh api failed: gh: Server Error (HTTP 5020)",
        "gh api failed: gh: Server Error HTTP 502",
        "other api failed: gh: Server Error (HTTP 502)",
        "prefix gh api failed: gh: Server Error (HTTP 502)",
        "Logical state inconsistency",
        "Task provenance mismatch (HTTP 502)",
    ],
)
def test_run_metadata_unclassified_errors_propagate(
    run_refresh_discovery: Any, message: str
) -> None:
    gh, _, saved, _ = run_refresh_discovery
    error = RuntimeError(message)
    gh.errors["actions/runs/1"] = error
    with pytest.raises(RuntimeError) as caught:
        controller.discover(gh, 17)
    assert caught.value is error
    assert saved == []


@pytest.mark.parametrize("malformed", ["metadata", "state"])
def test_run_refresh_malformed_metadata_and_state_fail(
    run_refresh_discovery: Any, malformed: str
) -> None:
    gh, state, saved, _ = run_refresh_discovery
    if malformed == "metadata":
        gh.run_metadata["1"] = {"id": 1}
    else:
        del state["runs"]["1"]["status"]
    with pytest.raises(KeyError, match="status"):
        controller.discover(gh, 17)
    assert saved == []


@pytest.mark.parametrize("operation", ["record", "jobs", "permission"])
def test_run_refresh_5xx_catch_does_not_cover_other_operations(
    run_refresh_discovery: Any, monkeypatch: Any, operation: str
) -> None:
    gh, _, saved, _ = run_refresh_discovery
    error = RuntimeError("gh api failed: gh: Server Error (HTTP 502)")
    if operation == "record":

        def fail(*args: Any) -> None:
            raise error

        monkeypatch.setattr(controller, "_record_run", fail)
    elif operation == "jobs":
        gh.run_metadata["1"].update(status="completed", conclusion="success")
        gh.errors["actions/runs/1/jobs?filter=latest&per_page=100"] = error
    else:
        gh.errors["collaborators/owner/permission"] = error
    with pytest.raises(RuntimeError) as caught:
        controller.discover(gh, 17)
    assert caught.value is error
    assert saved == []
