from __future__ import annotations

import json
import os
import subprocess
from functools import partial
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest

from tests.repository.github_agent import controller, owned_pr

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def control(tmp_path: Any, monkeypatch: Any) -> Any:
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    return controller.trusted_module("review_control")


class GitHub:
    repository = "owner/repo"

    def __init__(self) -> None:
        self.pr = owned_pr()
        self.pr.update(title="Change export scope", body="", labels=[])
        self.comments: list[dict[str, Any]] = []

    def pages(self, path: str) -> list[dict[str, Any]]:
        return self.comments

    def comment(self, number: int, body: str) -> None:
        self.comments.append({"body": body, "user": {"type": "Bot", "login": "app"}})

    def api(self, path: str, method: str = "GET", data: Any = None) -> Any:
        if path.endswith("/labels") and method == "POST":
            self.pr["labels"] = [{"name": x} for x in data["labels"]]
        if method == "DELETE":
            self.pr["labels"] = []
        return self.pr["labels"]

    def pull(self, number: int) -> dict[str, Any]:
        return self.pr


def publication_command(real_command: Any, pushed: list[str], *args: str, **kwargs: Any) -> Any:
    if args[:2] == ("git", "push"):
        pushed.append(args[-1].split(":", 1)[0])
        return ""
    if "commit-tree" in args:
        args = tuple(arg for arg in args if arg != "-S")
    return real_command(*args, **kwargs)


def publication_history(
    repository: tuple[Path, Any, Any],
    path: str = ".github/agent/controller.py",
    *,
    initial: bytes | None = b"original\n",
    proposal: bytes | None = None,
    dev: bytes | None = b"immutable dev\n",
) -> tuple[str, str, str]:
    work, git, commit = repository
    protected = work / path
    protected.parent.mkdir(parents=True, exist_ok=True)
    if initial is not None:
        protected.write_bytes(initial)
    ancestor = commit()
    (work / "feature.txt").write_bytes(b"requested feature\n")
    if proposal is not None:
        protected.write_bytes(proposal)
    head = commit()
    git("checkout", "--detach", ancestor)
    if dev is None:
        protected.unlink()
    else:
        protected.write_bytes(dev)
    base = commit()
    git("checkout", "--detach", head)
    protected.parent.mkdir(parents=True, exist_ok=True)
    if dev is None:
        protected.unlink()
    else:
        protected.write_bytes(dev)
    git("add", "--all")
    return head, base, ancestor


@pytest.fixture
def publication(tmp_path: Path, monkeypatch: Any, control: Any) -> Any:
    work = tmp_path / "publication"
    work.mkdir()
    hooks = tmp_path / "empty-hooks"
    hooks.mkdir()
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
    real_run = subprocess.run

    def isolated_run(*args: Any, **kwargs: Any) -> Any:
        kwargs["env"] = env
        return real_run(*args, **kwargs)

    monkeypatch.setattr(controller.subprocess, "run", isolated_run)

    def git(*args: str, data: bytes | None = None) -> bytes:
        return real_run(
            ["git", *args],
            cwd=work,
            env=env,
            input=data,
            capture_output=True,
            check=True,
            timeout=30,
        ).stdout

    git("init")
    for key, value in (
        ("user.name", "Publication regression"),
        ("user.email", "publication@example.invalid"),
        ("commit.gpgsign", "false"),
        ("core.hooksPath", hooks.as_posix()),
        ("core.autocrlf", "false"),
        ("core.eol", "lf"),
        ("core.filemode", "false"),
    ):
        git("config", "--local", key, value)
    proto = controller.trusted_module("task_context")
    context = {
        "version": 1,
        "task_id": "publication",
        "producer_id": "author:test",
        "source": "user",
        "starting_prompt": "Preserve the requested feature.",
        "corrections": [],
    }
    gh = GitHub()
    gh.pr["body"] = proto.render(context)
    gh.base = Mock()
    gh.label = Mock()
    pushed: list[str] = []
    real_command = controller.command

    monkeypatch.setattr(controller, "command", partial(publication_command, real_command, pushed))
    monkeypatch.setattr(controller, "signing_options", list)
    monkeypatch.setattr(controller, "signed_head", lambda *_args: True)
    monkeypatch.setattr(controller, "dispatch", Mock())
    monkeypatch.setenv("AGENT_CONTROL_DIR", str(work))

    def commit() -> str:
        git("add", "--all")
        git("commit", "--allow-empty", "-m", "Fixture" + proto.trailers(context))
        return git("rev-parse", "HEAD").decode("ascii").strip()

    def task(head: str, base: str, kind: str = "conflict") -> dict[str, Any]:
        gh.pr["head"]["sha"] = head
        gh.base.return_value = base
        return {
            "number": gh.pr["number"],
            "kind": kind,
            "head": head,
            "base": base,
            "branch": gh.pr["head"]["ref"],
            "user_task": context,
            "producer_id": context["producer_id"],
            "task_digest": proto.digest(context),
        }

    def artifact(value: dict[str, Any], *, empty: bool = False) -> Path:
        output = tmp_path / "publication-artifact"
        if empty:
            output.mkdir(exist_ok=True)
            (output / "task.json").write_text(json.dumps(value))
            (output / "fix.patch").write_bytes(b"")
        else:
            controller.collect(value, work, output)
            git("reset", "--hard", value["head"])
        if value["kind"] == "repair" and not empty:
            (output / "verdict.json").write_text(
                json.dumps({"outcome": "patch", "reason": "repair", "required_evidence": []})
            )
        return output

    history = partial(publication_history, (work, git, commit))
    return work, git, commit, task, artifact, gh, pushed, history
