from __future__ import annotations

import base64
import copy
import json
import os
import subprocess
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest

from tests.repository._support.publication import GitHub
from tests.repository._support.publication import (
    control as control,  # noqa: PLC0414 - pytest fixture export
)
from tests.repository._support.publication import (
    publication as publication,  # noqa: PLC0414 - pytest fixture export
)
from tests.repository.github_agent import controller

if TYPE_CHECKING:
    from pathlib import Path


def test_rejection_is_durable_head_changes_do_not_resume_and_alert_has_no_buttons(
    control: Any,
) -> None:
    gh = GitHub()
    control.pause(gh, gh.pr, "Prompt allows CSV; patch adds SQL writes.", "Added database writes")
    assert control.paused(gh, gh.pr)
    gh.pr["head"]["sha"] = "b" * 40
    assert control.paused(gh, gh.pr)
    events = control.pending()
    assert len(events) == 1
    assert events[0]["token"] is None
    gh.comments.append(
        {
            "body": "/agent resume " + "b" * 40,
            "user": {"type": "User", "login": "owner"},
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    assert not control.paused(gh, gh.pr)


def test_callback_requires_owner_channel_message_and_current_head(control: Any) -> None:
    gh = GitHub()
    proto = controller.trusted_module("task_context")
    task = proto.repair_context("task", "author:test")
    control.assess(
        gh.repository,
        1,
        gh.pr["head"]["sha"],
        proto.digest(task),
        "Protect signed PR review and owner approval.",
    )
    control.approval_notice(gh, gh.pr, [".github/agent/controller.py"], task)
    event = control.pending()[0]
    control.delivery(event["key"], message_id=7)
    query = {
        "id": "callback-1",
        "data": "approve:" + event["token"],
        "from": {"id": 22},
        "message": {"message_id": 7, "chat": {"id": -1001}},
    }
    with pytest.raises(ValueError, match="configured Telegram owner"):
        control.callback(gh, query, 33, -1001)
    with pytest.raises(ValueError, match="configured Telegram owner"):
        control.callback(gh, query, 22, -1002)
    gh.pr["head"]["sha"] = "b" * 40
    with pytest.raises(ValueError, match="PR changed"):
        control.callback(gh, query, 22, -1001)
    gh.pr["head"]["sha"] = "a" * 40
    assert "separate review" in control.callback(gh, query, 22, -1001)
    assert control.approved(gh.repository, 1, "a" * 40)
    assert not control.approved(gh.repository, 1, "b" * 40)
    assert "/agent approve-policy " + "a" * 40 in gh.comments[0]["body"]
    query["data"] = "disapprove:" + event["token"]
    query["id"] = "callback-2"
    control.callback(gh, query, 22, -1001)
    assert not control.approved(gh.repository, 1, "a" * 40)
    assert control.paused(gh, gh.pr)


def test_retry_outbox_deduplicates_event_and_keeps_actionable_explanation(control: Any) -> None:
    control.enqueue("one", "rejection", {"changes": "Expanded scope", "reason": "Not requested"})
    control.enqueue("one", "rejection", {"changes": "Duplicate"})
    event = control.pending()[0]
    assert json.loads(event["payload"])["reason"] == "Not requested"
    control.delivery("one", delay=30)
    assert not control.pending()
    control.delivery("one", message_id=10)
    assert not control.pending()


@pytest.mark.parametrize(
    "path",
    [
        "AGENTS.md",
        "agent_docs/sql.md",
        "release_routines/sql_integration.py",
        ".github/workflows/sql-integration.yml",
        "agent_tools/sessions.py",
    ],
)
def test_normal_policy_and_test_repairs_no_longer_require_owner_comment(path: str) -> None:
    assert not controller.policy_path(path)


@pytest.mark.parametrize(
    "path",
    [
        ".github/agent/prompts/review.md",
        ".github/required-workflows.json",
        ".github/workflows/agent-dispatch.yml",
        ".codex/config.toml",
    ],
)
def test_controller_security_paths_still_require_owner_comment(path: str) -> None:
    assert controller.policy_path(path)


def test_review_execution_must_be_fresh_read_only_and_bound_to_task() -> None:
    task = {
        "kind": "review",
        "head": "a" * 40,
        "base": "b" * 40,
        "task_digest": "c" * 64,
        "producer_id": "author:one",
    }
    receipt = {**task, "mode": "read-only", "reviewer_id": "reviewer:two"}
    controller.verify_review_execution(task, receipt)
    for changed in (
        {"head": "d" * 40},
        {"task_digest": "e" * 64},
        {"mode": "workspace-write"},
        {"reviewer_id": "author:one"},
    ):
        with pytest.raises(RuntimeError):
            controller.verify_review_execution(task, {**receipt, **changed})


def test_app_features_need_host_enrollment_and_exact_task_context(control: Any) -> None:
    gh = GitHub()
    pr = copy.deepcopy(gh.pr)
    pr["user"] = {"type": "Bot", "login": "analytics-toolkit-agent-717959[bot]"}
    pr["head"]["ref"] = "codex/telegram-feature-owner-task"
    assert not controller.trusted(pr, gh.repository)
    control.enroll_feature(gh.repository, pr["head"]["ref"], "a" * 64)
    assert controller.trusted(pr, gh.repository)
    assert control.enrolled_feature(gh.repository, pr["head"]["ref"], "a" * 64)
    assert not control.enrolled_feature(gh.repository, pr["head"]["ref"], "b" * 64)
    other = copy.deepcopy(pr)
    other["head"]["ref"] = "codex/telegram-feature-attacker"
    assert not controller.trusted(other, gh.repository)


def test_owner_correction_holds_existing_pr_before_replanning(control: Any) -> None:
    gh = GitHub()
    gh.pr["head"]["ref"] = "codex/telegram-feature-task"
    control.enroll_feature(gh.repository, gh.pr["head"]["ref"], "a" * 64)
    control.hold_feature(gh, gh.pr)
    assert control.paused(gh, gh.pr)
    assert not control.pending()  # A user correction is not a reviewer rejection.
    control.resume_feature(gh, gh.pr)
    assert not control.paused(gh, gh.pr)


def test_publication_recovery_does_not_clear_a_real_reviewer_rejection(control: Any) -> None:
    gh = GitHub()
    gh.pr["head"]["ref"] = "codex/telegram-feature-task"
    control.enroll_feature(gh.repository, gh.pr["head"]["ref"], "a" * 64)
    control.pause(gh, gh.pr, "Scope does not match the user request.", "Unrequested API")
    control.recover_feature_publication(gh, gh.pr)
    assert control.paused(gh, gh.pr)
    with control.connect() as db:
        db.execute(
            "UPDATE pauses SET reason=?",
            ("PR task context is not bound to its signed head commit.",),
        )
    control.recover_feature_publication(gh, gh.pr)
    assert not control.paused(gh, gh.pr)


def test_success_notifications_require_real_merge_promotion_and_production_publish(
    control: Any,
) -> None:
    gh = Mock(repository="owner/repo")
    main = ["a" * 40]
    promotion = ["diverged"]
    pr = {
        "number": 5,
        "title": "CSV export",
        "head": {"sha": "b" * 40},
        "merge_commit_sha": "c" * 40,
        "merged_at": "2026-10-12T10:00:00Z",
        "html_url": "https://github.com/owner/repo/pull/5",
    }
    run = {
        "id": 8,
        "created_at": "2026-10-12T11:00:00Z",
        "head_sha": "d" * 40,
        "html_url": "https://github.com/owner/repo/actions/runs/8",
    }
    jobs = [{"name": "publish to TestPyPI", "conclusion": "success"}]

    def api(path: str) -> Any:
        if path == "git/ref/heads/main":
            return {"object": {"sha": main[0]}}
        if path.startswith("compare/"):
            return {"status": promotion[0], "commits": [{"commit": {"message": "CSV export"}}]}
        if path.startswith("commits/"):
            return {"sha": "d" * 40}
        return {
            "content": base64.b64encode(
                b"## 1.2.3.4 - 2026-10-12\n\n- Added CSV export.\n"
            ).decode()
        }

    def pages(path: str, key: str | None = None) -> Any:
        if path.startswith("pulls?"):
            return [pr]
        if path.startswith("actions/workflows/"):
            return [run]
        if path.endswith("/jobs?per_page=100"):
            return jobs
        return [
            {
                "tag_name": "v1.2.3.4",
                "published_at": "2026-10-12T11:01:00Z",
                "draft": False,
                "html_url": "https://github.com/owner/repo/releases/tag/v1.2.3.4",
            }
        ]

    gh.api.side_effect = api
    gh.pages.side_effect = pages
    gh.base.return_value = "e" * 40
    gh.checks.return_value = [
        {
            "name": "agent-review",
            "conclusion": "success",
            "app": {"id": 20},
            "output": {"summary": "What changed: Added CSV export.\n\nApproved"},
        }
    ]
    control.set_cursor("owner/repo:notifications:start", "2026-10-01T00:00:00Z")
    control.set_cursor("owner/repo:notifications:main", "f" * 40)
    control.observe(gh, 20)
    assert [event["kind"] for event in control.pending()] == ["merged"]
    main[0] = "9" * 40
    promotion[0] = "identical"
    jobs[0]["name"] = "publish to PyPI"
    control.observe(gh, 20)
    control.observe(gh, 20)
    events = control.pending()
    assert [event["kind"] for event in events] == ["merged", "main", "release"]
    assert "Added CSV export" in json.loads(events[-1]["payload"])["changes"]
    assert all(event["token"] is None for event in events)


def test_command_raw_stdout_is_opt_in(monkeypatch: Any, tmp_path: Path) -> None:
    payload = b"  patch\r\n \r\n\xff\n"
    run = Mock(
        side_effect=[
            subprocess.CompletedProcess(["git", "diff"], 0, "  patch\n \n", ""),
            subprocess.CompletedProcess(["git", "diff"], 0, payload, b""),
        ]
    )
    monkeypatch.setattr(controller.subprocess, "run", run)
    data = "request \u00e9"
    assert controller.command("git", "diff", cwd=tmp_path, data=data) == "patch"
    assert controller.command("git", "diff", cwd=tmp_path, data=data, raw_stdout=True) == payload
    env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")
    for call, raw in zip(run.call_args_list, (False, True)):
        assert call.args == (("git", "diff"),)
        assert call.kwargs == {
            "cwd": tmp_path,
            "input": data.encode("utf-8") if raw else data,
            "text": not raw,
            "capture_output": True,
            "timeout": 180,
            "env": env,
        }


@pytest.mark.parametrize("raw", [False, True])
def test_command_errors_remain_bounded_and_readable(monkeypatch: Any, raw: bool) -> None:
    stderr = b"bad \xff\r\n" + b"x" * 8000 if raw else "bad\n" + "x" * 8000
    result = subprocess.CompletedProcess(["git", "diff"], 1, b"" if raw else "", stderr)
    run = Mock(return_value=result)
    monkeypatch.setattr(controller.subprocess, "run", run)
    with pytest.raises(RuntimeError) as error:
        controller.command("git", "diff", raw_stdout=raw)
    expected = ("bad \ufffd\r\n" if raw else "bad\n") + "x" * 8000
    assert str(error.value) == "git diff failed: " + expected[:1800]
    assert run.call_args.kwargs["input"] is None
    assert run.call_args.kwargs["text"] is not raw


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"], ids=["lf", "crlf"])
def test_collected_patch_preserves_blank_context_and_applies(
    tmp_path: Path, monkeypatch: Any, newline: bytes
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)

    def git(*args: str) -> bytes:
        return subprocess.run(
            ["git", *args],
            cwd=work,
            env=env,
            capture_output=True,
            check=True,
            timeout=30,
        ).stdout

    real_run = subprocess.run

    def isolated_run(*args: Any, **kwargs: Any) -> Any:
        kwargs["env"] = dict(
            kwargs["env"], GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull
        )
        return real_run(*args, **kwargs)

    monkeypatch.setattr(controller.subprocess, "run", isolated_run)
    git("init")
    for key, value in (
        ("user.name", "Patch regression"),
        ("user.email", "patch@example.invalid"),
        ("commit.gpgsign", "false"),
        ("core.hooksPath", hooks.as_posix()),
        ("core.autocrlf", "false"),
        ("core.eol", "lf"),
        ("core.whitespace", "cr-at-eol"),
    ):
        git("config", "--local", key, value)
    fixture = work / "example.txt"
    original = newline.join((b"before", b"unchanged", b"", b""))
    intended = newline.join((b"after", b"unchanged", b"", b""))
    fixture.write_bytes(original)
    git("add", "--all")
    git("commit", "-m", "Baseline")
    head = git("rev-parse", "HEAD").decode("ascii").strip()
    fixture.write_bytes(intended)
    git("add", "--all")
    expected = git("diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv", head)
    assert expected.endswith(b" " + newline)
    task = {"kind": "repair", "head": head}
    output = tmp_path / "artifacts"
    controller.collect(task, work, output)
    patch = output / "fix.patch"
    assert patch.read_bytes() == expected
    assert patch.read_bytes().endswith(b" " + newline)
    assert json.loads((output / "task.json").read_text()) == task
    git("reset", "--hard", head)
    assert fixture.read_bytes() == original
    git("apply", "--check", "--index", str(patch))
    git("apply", "--index", str(patch))
    assert fixture.read_bytes() == intended
    assert git("show", ":example.txt") == intended


def test_review_collection_has_metadata_without_patch_or_git(
    tmp_path: Path, monkeypatch: Any
) -> None:
    command = Mock(side_effect=AssertionError("Review collection must not invoke Git"))
    monkeypatch.setattr(controller, "command", command)
    task = {"kind": "review", "head": "a" * 40}
    output = tmp_path / "artifacts"
    controller.collect(task, tmp_path, output)
    assert json.loads((output / "task.json").read_text()) == task
    assert not (output / "fix.patch").exists()
    command.assert_not_called()


def test_collection_rejects_unresolved_conflicts_before_serialization(
    tmp_path: Path, monkeypatch: Any
) -> None:
    command = Mock(side_effect=["", "", "unmerged entry"])
    monkeypatch.setattr(controller, "command", command)
    output = tmp_path / "artifacts"
    with pytest.raises(RuntimeError, match="Unresolved conflicts"):
        controller.collect({"kind": "repair", "head": "a" * 40}, tmp_path, output)
    assert command.call_count == 3
    assert not (output / "fix.patch").exists()
    assert not (output / "task.json").exists()


PROTECTED_PATHS = [".github/agent/controller.py", ".github/workflows/github-agent.yml"]
DEVIATIONS = ["content", "mode", "repair", "discard", "discard-updated"]
DEVIATIONS += [".env", ".connections", ".git/config"]
METADATA_FAILURES = ["unavailable", "unresolved", "ambiguous", "missing-blob", "malformed"]


@pytest.mark.parametrize("path", PROTECTED_PATHS)
@pytest.mark.parametrize("change", ["update", "addition", "deletion"])
def test_conflict_publishes_only_exact_inherited_dev_entries(
    publication: Any, path: str, change: str
) -> None:
    work, git, _commit, task, artifact, gh, pushed, history = publication
    head, base, _ancestor = history(
        path,
        initial=None if change == "addition" else b"original\n",
        dev=None if change == "deletion" else b"immutable dev\n",
    )
    value = task(head, base)
    output = artifact(value)
    controller.publish(gh, value, output, work)
    assert len(pushed) == 1
    expected = git("ls-tree", "-z", base, "--", path)
    assert git("ls-tree", "-z", pushed[0], "--", path) == expected
    assert git("ls-files", "--stage", "--", path).strip() == (
        expected.replace(b" blob ", b" ").replace(b"\t", b" 0\t").rstrip(b"\0")
    )
    assert git("show", pushed[0] + ":feature.txt") == b"requested feature\n"
    gh.label.assert_called_once_with(value["number"], acquire=False)


@pytest.mark.parametrize("deviation", DEVIATIONS)
def test_conflict_denies_authored_edits_repairs_and_discarded_feature_proposals(
    publication: Any, monkeypatch: Any, deviation: str
) -> None:
    work, git, _commit, task, artifact, gh, pushed, history = publication
    path = deviation if deviation in {".env", ".connections"} else ".github/agent/controller.py"
    head, base, _ancestor = history(
        path,
        proposal=b"feature-owned proposal\n" if deviation.startswith("discard") else None,
        dev=b"original\n" if deviation == "discard" else b"immutable dev\n",
    )
    if deviation == "content":
        (work / path).write_bytes(b"worker edit\n")
    git("add", "--all")
    if deviation == "mode":
        git("update-index", "--chmod=+x", path)
    value = task(head, base, "repair" if deviation == "repair" else "conflict")
    output = artifact(value)
    if deviation == ".git/config":  # Git cannot track its own metadata directory.
        original = controller.command

        def command(*args: str, **kwargs: Any) -> Any:
            if args[:4] == ("git", "diff", "--cached", "--name-only"):
                return deviation
            return original(*args, **kwargs)

        monkeypatch.setattr(controller, "command", command)
    with pytest.raises(RuntimeError, match="controller or sensitive"):
        controller.publish(gh, value, output, work)
    assert not pushed
    assert "GitHub agent blocker:" in gh.comments[-1]["body"]
    gh.label.assert_called_once_with(value["number"], acquire=False)


@pytest.mark.parametrize("failure", METADATA_FAILURES)
def test_conflict_history_and_index_fail_closed(
    publication: Any, monkeypatch: Any, failure: str
) -> None:
    work, git, _commit, task, artifact, gh, pushed, history = publication
    path = ".github/agent/controller.py"
    head, base, ancestor = history(dev=b"dev\n")
    if failure == "ambiguous":
        tree = git("rev-parse", head + "^{tree}").decode("ascii").strip()
        proto = controller.trusted_module("task_context")
        message = "Merge fixture" + proto.trailers(task(head, base)["user_task"])
        left = git("commit-tree", tree, "-p", head, "-p", base, "-m", message).decode().strip()
        right = git("commit-tree", tree, "-p", base, "-p", head, "-m", message).decode().strip()
        head, base = left, right
        git("reset", "--hard", head)
        (work / path).write_bytes(b"dev\n")
        git("add", "--all")
    value = task(head, "f" * 40 if failure == "unavailable" else base)
    output = artifact(value, empty=True)
    if failure in {"unresolved", "missing-blob"}:
        oid = git("rev-parse", ancestor + ":" + path).decode().strip()
        git("update-index", "--force-remove", path)
        if failure == "unresolved":
            records = "".join(
                "100644 " + oid + " " + str(stage) + "\t" + path + "\n" for stage in (1, 2, 3)
            )
            git("update-index", "--index-info", data=records.encode())
        else:
            git("update-index", "--add", "--cacheinfo", "100644," + "e" * 40 + "," + path)
    if failure == "malformed":
        original = controller.command

        def command(*args: str, **kwargs: Any) -> Any:
            return b"invalid\0" if args[:2] == ("git", "ls-tree") else original(*args, **kwargs)

        monkeypatch.setattr(controller, "command", command)
    with pytest.raises(RuntimeError):
        controller.publish(gh, value, output, work)
    assert not pushed
    gh.label.assert_called_once_with(value["number"], acquire=False)


def test_opaque_patch_collect_candidate_and_publish_keep_original_bytes(publication: Any) -> None:
    work, git, commit, task, artifact, gh, pushed, _history = publication
    fixture = work / "opaque.txt"
    fixture.write_bytes(b"before\xff\nunchanged\n\n")
    head = commit()
    intended = b"after\xff\nunchanged\n\n"
    fixture.write_bytes(intended)
    git("add", "--all")
    expected = git("diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv", head)
    assert b"\xff" in expected
    value = task(head, head)
    output = artifact(value)
    patch = output / "fix.patch"
    assert patch.read_bytes() == expected
    candidate = controller.candidate(gh, value, output, work)
    assert git("show", candidate + ":opaque.txt") == intended
    assert git("ls-tree", candidate, "--", "opaque.txt").startswith(b"100644 blob ")
    git("reset", "--hard", head)
    controller.publish(gh, value, output, work)
    assert git("show", pushed[-1] + ":opaque.txt") == intended
    assert git("show", ":opaque.txt") == intended
    assert patch.read_bytes() == expected
