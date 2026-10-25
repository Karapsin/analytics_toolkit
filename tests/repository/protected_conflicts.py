from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest

from tests.repository._support.protected_conflicts import (
    assert_stopped,
    discover_again,
    preparation,
)
from tests.repository._support.publication import (
    control as control,  # noqa: PLC0414 - pytest fixture
)
from tests.repository._support.publication import (
    publication as publication,  # noqa: PLC0414 - pytest fixture
)
from tests.repository.github_agent import controller

if TYPE_CHECKING:
    from pathlib import Path


def test_blocked_protected_merge_durably_routes_to_fresh_correction(
    publication: Any, control: Any, monkeypatch: Any
) -> None:
    work, git, _commit, task, artifact, gh, pushed, history = publication
    path = ".github/agent/controller.py"
    head, base, _ancestor = history(proposal=b"existing feature proposal\n")
    (work / path).write_bytes(b"resolution combining feature and dev\n")
    git("add", "--all")
    value = task(head, base)
    output = artifact(value)

    with pytest.raises(RuntimeError, match="fresh correction planning queued"):
        controller.publish(gh, value, output, work)
    assert not pushed
    assert git("rev-parse", "HEAD").decode().strip() == head
    request = control.correction(gh.repository, 1, head, value["task_digest"])
    assert request
    assert path in request["reason"]
    assert not control.pending()
    assert not control.paused(gh, gh.pr)
    assert not control.approved(gh.repository, 1, head)
    assert not control.correction(gh.repository, 1, "f" * 40, value["task_digest"])
    assert not control.correction(gh.repository, 1, head, "f" * 64)
    assert all("Reviewer requested changes" not in c["body"] for c in gh.comments)

    # A new controller instance must see the durable request after restart.
    monkeypatch.setattr(controller, "MODULES", {})
    reloaded = controller.trusted_module("review_control")
    assert reloaded.correction(gh.repository, 1, head, value["task_digest"]) == request
    original_api = gh.api

    def api(endpoint: str, method: str = "GET", data: Any = None) -> Any:
        if endpoint.startswith("collaborators/"):
            return {"permission": "write"}
        if endpoint.startswith("compare/"):
            return {"behind_by": 1}
        return original_api(endpoint, method, data)

    monkeypatch.setattr(gh, "api", api)
    monkeypatch.setattr(
        gh,
        "pages",
        lambda endpoint, key=None: gh.comments if endpoint.startswith("issues/") else [],
    )
    monkeypatch.setattr(gh, "pulls", Mock(return_value=[gh.pr]), raising=False)
    monkeypatch.setattr(gh, "checks", Mock(return_value=[]), raising=False)
    monkeypatch.setattr(
        controller,
        "monitor",
        lambda _gh: (
            1,
            {"started": "2026-01-01T00:00:00Z", "runs": {}, "failures": {}, "candidates": {}},
        ),
    )
    monkeypatch.setattr(controller, "save_monitor", Mock())
    tasks = controller.discover(gh, 17)["include"]
    assert len(tasks) == 1
    assert tasks[0]["kind"] == "correction"
    assert tasks[0]["head"] == head
    assert tasks[0]["base"] == base
    assert tasks[0]["task_digest"] == value["task_digest"]
    assert tasks[0]["user_task"] == value["user_task"]
    assert tasks[0]["rejection"] == request
    assert not reloaded.pending()


def test_sensitive_path_cannot_create_autonomous_protected_correction(
    publication: Any, control: Any
) -> None:
    work, git, _commit, task, artifact, gh, pushed, history = publication
    head, base, _ancestor = history(proposal=b"feature controller proposal\n")
    (work / ".github/agent/controller.py").write_bytes(b"protected resolution\n")
    (work / ".env").write_text("fake fixture configuration\n")
    git("add", "--all")
    value = task(head, base)
    output = artifact(value)
    with pytest.raises(RuntimeError, match="controller or sensitive"):
        controller.publish(gh, value, output, work)
    assert not pushed
    assert not control.correction(gh.repository, 1, head, value["task_digest"])
    assert not control.pending()


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("changed", ["head", "digest", "findings"])
def test_inflight_conflict_cannot_replace_concurrent_correction(
    publication: Any, control: Any, monkeypatch: Any, existing: bool, changed: str
) -> None:
    work, git, _commit, task, artifact, gh, pushed, history = publication
    head, base, _ancestor = history(proposal=b"feature controller proposal\n")
    (work / ".github/agent/controller.py").write_bytes(b"protected resolution\n")
    git("add", "--all")
    value = task(head, base)
    output = artifact(value)
    if existing:
        control.require_correction(
            gh.repository,
            1,
            "b" * 40,
            "e" * 64,
            "Old reason",
            "Old assessment",
            expected=None,
        )
    newer_head = "f" * 40 if changed == "head" else head
    newer_digest = "f" * 64 if changed == "digest" else value["task_digest"]
    inherited = controller.inherited_controller_path

    def inspect(*args: Any) -> bool:
        with control.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO corrections VALUES (?,?,?,?,?,?,?)",
                (
                    gh.repository,
                    1,
                    newer_head,
                    newer_digest,
                    "New findings",
                    "New assessment",
                    99.0,
                ),
            )
        return inherited(*args)

    monkeypatch.setattr(controller, "inherited_controller_path", inspect)
    message = "fresh correction planning queued" if changed == "findings" else "findings changed"
    with pytest.raises(RuntimeError, match=message):
        controller.publish(gh, value, output, work)
    assert control.correction(gh.repository, 1, newer_head, newer_digest) == {
        "reason": "New findings",
        "changes": "New assessment",
    }
    assert not pushed
    assert not control.pending()


@pytest.mark.parametrize("change", ["head", "hold"])
def test_routing_rechecks_authority_after_protected_history_inspection(
    publication: Any, control: Any, monkeypatch: Any, change: str
) -> None:
    work, git, _commit, task, artifact, gh, pushed, history = publication
    head, base, _ancestor = history(proposal=b"feature controller proposal\n")
    (work / ".github/agent/controller.py").write_bytes(b"protected resolution\n")
    git("add", "--all")
    value = task(head, base)
    output = artifact(value)
    inherited = controller.inherited_controller_path

    def inspect(*args: Any) -> bool:
        if change == "head":
            gh.pr["head"]["sha"] = "f" * 40
        else:
            with control.connect() as db:
                db.execute(
                    "INSERT INTO pauses VALUES (?,?,?,?,?)",
                    (gh.repository, 1, head, "Owner/security hold", 1.0),
                )
        return inherited(*args)

    monkeypatch.setattr(controller, "inherited_controller_path", inspect)
    message = "PR changed" if change == "head" else "awaiting user"
    with pytest.raises(RuntimeError, match=message):
        controller.publish(gh, value, output, work)
    assert not control.correction_snapshot(gh.repository, 1)
    assert not pushed
    assert not control.pending()


@pytest.mark.parametrize("stale", [None, "head", "digest"])
def test_routing_preserves_current_findings_replaces_stale_scope_and_retains_hold(
    tmp_path: Path, monkeypatch: Any, stale: str | None
) -> None:
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    controls = controller.trusted_module("review_control")
    old_head = "b" * 40 if stale == "head" else "a" * 40
    old_digest = "e" * 64 if stale == "digest" else "d" * 64
    controls.require_correction(
        "owner/repo",
        1,
        old_head,
        old_digest,
        "Existing source findings",
        "Original assessment",
        expected=None,
    )
    with controls.connect() as db:
        db.execute(
            "INSERT INTO pauses VALUES (?,?,?,?,?)",
            ("owner/repo", 1, "a" * 40, "Owner/security hold", 1.0),
        )
    controls.require_correction(
        "owner/repo",
        1,
        "a" * 40,
        "d" * 64,
        "Protected merge requires planning",
        "Routing assessment",
        expected=controls.correction_snapshot("owner/repo", 1),
    )
    expected = (
        {"reason": "Protected merge requires planning", "changes": "Routing assessment"}
        if stale
        else {"reason": "Existing source findings", "changes": "Original assessment"}
    )
    assert controls.correction("owner/repo", 1, "a" * 40, "d" * 64) == expected
    if stale:
        assert not controls.correction("owner/repo", 1, old_head, old_digest)
    with controls.connect() as db:
        row = db.execute("SELECT reason,created FROM pauses").fetchone()
        assert tuple(row) == ("Owner/security hold", 1.0)
    assert not controls.pending()
    assert not controls.approved("owner/repo", 1, "a" * 40)


@pytest.mark.parametrize(
    "scenario",
    [
        (".github/agent/controller.py", b"dev changes\n", None),
        (".github/workflows/github-agent.yml", b"dev changes\n", None),
        (".github/agent/delete.py", None, None),
        (".github/agent/odd\n\t name.py", b"dev changes\n", None),
        (".github/agent/original.py", b"dev changes\n", ".github/agent/renamed.py"),
        ("ordinary.py", b"dev changes\n", ".github/agent/renamed.py"),
    ],
)
def test_preparation_routes_real_protected_index_conflicts_before_worker(
    publication: Any,
    control: Any,
    monkeypatch: Any,
    scenario: tuple[str, bytes | None, str | None],
) -> None:
    path, _dev, rename = scenario
    case = preparation(publication, monkeypatch, scenario=scenario)
    untouched_head = case.git("rev-parse", "HEAD")
    with pytest.raises(RuntimeError, match="fresh correction planning queued"):
        controller.prepare(case.gh, case.task, case.work, case.control)
    assert_stopped(case, control)
    assert case.git("rev-parse", "HEAD") == untouched_head
    unresolved = case.git("diff", "--name-only", "--diff-filter=U", "-z").split(b"\0")[:-1]
    assert unresolved
    assert (rename or path).encode() in unresolved
    request = control.correction(case.gh.repository, 1, case.task["head"], case.task["task_digest"])
    assert request
    assert "independent review" in request["reason"]
    assert not control.paused(case.gh, case.gh.pr)

    # Restart and discover from the retained row, without collecting any worker result.
    monkeypatch.setattr(controller, "MODULES", {})
    reloaded = controller.trusted_module("review_control")
    assert (
        reloaded.correction(case.gh.repository, 1, case.task["head"], case.task["task_digest"])
        == request
    )
    tasks = discover_again(case, monkeypatch)
    assert len(tasks) == 1
    routed = tasks[0]
    assert routed["kind"] == "correction"
    for key in ("head", "base", "task_digest", "user_task", "producer_id"):
        assert routed[key] == case.task[key]
    assert routed["rejection"] == request


@pytest.mark.parametrize(
    ("path", "proposal"),
    [
        ("ordinary.py", b"feature proposal\n"),
        (".github/agent-adjacent.py", b"feature proposal\n"),
        (".github/workflows/github-agent.yml.bak", b"feature proposal\n"),
        (".github/agent/controller.py", None),
    ],
)
def test_preparation_retains_ordinary_conflicts_and_clean_protected_inheritance(
    publication: Any, control: Any, monkeypatch: Any, path: str, proposal: bytes | None
) -> None:
    case = preparation(
        publication, monkeypatch, scenario=(path, b"dev changes\n", None), proposal=proposal
    )
    controller.prepare(case.gh, case.task, case.work, case.control)
    case.ci.assert_called_once_with(case.gh, case.task)
    case.prompt.assert_called_once_with(case.task, case.control, [])
    assert not control.correction_snapshot(case.gh.repository, 1)
    if proposal is None:
        assert not case.git("ls-files", "--unmerged", "-z")
        assert controller.inherited_controller_path(case.task, case.work, path)
    else:
        assert case.git("ls-files", "--unmerged", "-z")
    assert not case.pushed


@pytest.mark.parametrize("path", [".env", ".connections"])
def test_preparation_refuses_sensitive_conflicts_without_requesting_correction(
    publication: Any, control: Any, monkeypatch: Any, path: str
) -> None:
    case = preparation(publication, monkeypatch, scenario=(path, b"dev changes\n", None))
    with pytest.raises(RuntimeError, match="sensitive"):
        controller.prepare(case.gh, case.task, case.work, case.control)
    assert_stopped(case, control)
    assert not control.correction_snapshot(case.gh.repository, 1)


@pytest.mark.parametrize(
    "raw",
    [
        b"missing terminator",
        b"\0",
        b"/absolute\0",
        b"a/../b\0",
        b"a//b\0",
        b"\xff\0",
        "not bytes",
    ],
)
def test_preparation_refuses_malformed_raw_inspection(
    publication: Any, control: Any, monkeypatch: Any, raw: Any
) -> None:
    case = preparation(publication, monkeypatch)
    command = controller.command

    def inspect(*args: Any, **kwargs: Any) -> Any:
        if "--diff-filter=U" in args:
            assert kwargs["raw_stdout"] is True
            assert "-z" in args
            return raw
        return command(*args, **kwargs)

    monkeypatch.setattr(controller, "command", inspect)
    with pytest.raises(RuntimeError, match=r"conflict paths"):
        controller.prepare(case.gh, case.task, case.work, case.control)
    assert_stopped(case, control)
    assert not control.correction_snapshot(case.gh.repository, 1)


def test_preparation_preserves_raw_nul_path_boundaries(publication: Any, monkeypatch: Any) -> None:
    case = preparation(publication, monkeypatch)
    command = controller.command
    paths = [".github/agent/new\nline.py", "tab\tfile.py", "space file.py"]

    def inspect(*args: Any, **kwargs: Any) -> Any:
        if "--diff-filter=U" in args:
            return b"\0".join(path.encode() for path in paths) + b"\0"
        return command(*args, **kwargs)

    monkeypatch.setattr(controller, "command", inspect)
    assert controller.unresolved_conflict_paths(case.work) == paths


@pytest.mark.parametrize("fault", ["inspection", "persistence", "replaced"])
def test_preparation_never_reports_queued_when_inspection_or_persistence_fails(
    publication: Any, control: Any, monkeypatch: Any, fault: str
) -> None:
    case = preparation(publication, monkeypatch)
    if fault == "inspection":
        monkeypatch.setattr(
            controller, "unresolved_conflict_paths", Mock(side_effect=RuntimeError("Git unavailable"))
        )
    else:
        monkeypatch.setattr(
            control,
            "require_correction",
            Mock(side_effect=OSError("Private store unavailable"))
            if fault == "persistence"
            else Mock(return_value=False),
        )
    with pytest.raises((RuntimeError, OSError)) as caught:
        controller.prepare(case.gh, case.task, case.work, case.control)
    assert "queued" not in str(caught.value)
    assert_stopped(case, control)
    assert not control.correction_snapshot(case.gh.repository, 1)


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("replacement", ["head", "digest", "findings"])
def test_preparation_preserves_concurrent_correction_rows(
    publication: Any, control: Any, monkeypatch: Any, existing: bool, replacement: str
) -> None:
    case = preparation(publication, monkeypatch)
    if existing:
        control.require_correction(
            case.gh.repository, 1, "b" * 40, "e" * 64, "Old findings", "Old scope", expected=None
        )
    inspect = controller.unresolved_conflict_paths
    newer_head = "f" * 40 if replacement == "head" else case.task["head"]
    newer_digest = "f" * 64 if replacement == "digest" else case.task["task_digest"]

    def replace(work: Path) -> list[str]:
        with control.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO corrections VALUES (?,?,?,?,?,?,?)",
                (case.gh.repository, 1, newer_head, newer_digest, "New findings", "New scope", 99.0),
            )
        return inspect(work)

    monkeypatch.setattr(controller, "unresolved_conflict_paths", replace)
    message = "planning queued" if replacement == "findings" else "findings changed"
    with pytest.raises(RuntimeError, match=message):
        controller.prepare(case.gh, case.task, case.work, case.control)
    assert_stopped(case, control)
    row = control.correction_snapshot(case.gh.repository, 1)
    assert row == {
        "repository": case.gh.repository,
        "number": 1,
        "head": newer_head,
        "digest": newer_digest,
        "reason": "New findings",
        "changes": "New scope",
        "created": 99.0,
    }


@pytest.mark.parametrize("change", ["head", "dev", "task", "signer", "veto", "hold", "coordinator"])
def test_preparation_rechecks_authority_after_index_inspection(
    publication: Any, control: Any, monkeypatch: Any, change: str
) -> None:
    case = preparation(publication, monkeypatch)
    inspect = controller.unresolved_conflict_paths
    original_metadata = controller.task_metadata

    def changed(work: Path) -> list[str]:
        if change == "head":
            case.gh.pr["head"]["sha"] = "f" * 40
        elif change == "dev":
            case.gh.base.return_value = "f" * 40
        elif change == "task":
            monkeypatch.setattr(
                controller,
                "task_metadata",
                lambda gh, pr: {**original_metadata(gh, pr), "task_digest": "f" * 64},
            )
        elif change == "signer":
            monkeypatch.setattr(controller, "signed_head", lambda *_args: False)
        elif change == "coordinator":
            control.hold_coordinator(
                case.gh,
                case.gh.pr,
                {"repository": case.gh.repository, "request_id": "dependency", "revision": 1},
            )
        else:
            with control.connect() as db:
                db.execute(
                    "INSERT INTO pauses VALUES (?,?,?,?,?)",
                    (case.gh.repository, 1, case.task["head"], "Owner/security hold", 1.0),
                )
                if change == "veto":
                    db.execute(
                        "INSERT INTO vetoes VALUES (?,?,?,?)",
                        (case.gh.repository, 1, case.task["head"], 1.0),
                    )
        return inspect(work)

    monkeypatch.setattr(controller, "unresolved_conflict_paths", changed)
    message = {
        "head": "PR changed",
        "dev": "Dev changed",
        "task": "Task context changed",
        "signer": "signed by an enrolled",
        "veto": "awaiting user",
        "hold": "awaiting user",
        "coordinator": "coordinator dependency",
    }[change]
    with pytest.raises(RuntimeError, match=message):
        controller.prepare(case.gh, case.task, case.work, case.control)
    assert_stopped(case, control)
    assert not control.correction_snapshot(case.gh.repository, 1)
    if change in {"veto", "hold", "coordinator"}:
        assert control.paused(case.gh, case.gh.pr)
    if change == "veto":
        assert control.veto(case.gh.repository, 1, case.task["head"])


@pytest.mark.parametrize(
    "path",
    ["agent_tools/sql_explorer_visual_render.py", ".github/workflows/agent-visual-profile.yml"],
)
@pytest.mark.parametrize("renamed_from", [False, True])
@pytest.mark.parametrize("adjacent", [False, True])
def test_composed_policy_checks_exact_paths_at_both_rename_endpoints(
    publication: Any, monkeypatch: Any, path: str, renamed_from: bool, adjacent: bool
) -> None:
    _work, _git, _commit, task, _artifact, gh, _pushed, history = publication
    head, base, _ancestor = history()
    task(head, base)
    endpoint = path + ".bak" if adjacent else path
    files = [
        {"filename": "ordinary.py", "previous_filename": endpoint}
        if renamed_from
        else {"filename": endpoint, "previous_filename": "ordinary.py"}
    ]
    monkeypatch.setattr(
        gh, "pages", lambda endpoint: files if endpoint.startswith("pulls/") else []
    )
    denial = controller.authorization_error(gh, gh.pr)
    assert bool(denial) is not adjacent
    assert controller.policy_path(path)
    assert not controller.policy_path(path + ".bak")


def test_preparation_retains_existing_same_scope_findings(
    publication: Any, control: Any, monkeypatch: Any
) -> None:
    case = preparation(publication, monkeypatch)
    assert control.require_correction(
        case.gh.repository,
        1,
        case.task["head"],
        case.task["task_digest"],
        "Existing independent findings",
        "Complete original scope",
        expected=None,
    )
    before = control.correction_snapshot(case.gh.repository, 1)
    with pytest.raises(RuntimeError, match="planning queued"):
        controller.prepare(case.gh, case.task, case.work, case.control)
    assert_stopped(case, control)
    assert control.correction_snapshot(case.gh.repository, 1) == before


@pytest.mark.parametrize("path", [".git", ".git/private", ".env", ".connections"])
def test_sensitive_unresolved_member_takes_precedence_over_protected_routing(
    publication: Any, control: Any, monkeypatch: Any, path: str
) -> None:
    case = preparation(publication, monkeypatch)
    command = controller.command

    def inspect(*args: Any, **kwargs: Any) -> Any:
        if "--diff-filter=U" in args:
            return b".github/agent/controller.py\0" + path.encode() + b"\0"
        return command(*args, **kwargs)

    monkeypatch.setattr(controller, "command", inspect)
    with pytest.raises(RuntimeError, match="sensitive"):
        controller.prepare(case.gh, case.task, case.work, case.control)
    assert_stopped(case, control)
    assert not control.correction_snapshot(case.gh.repository, 1)
