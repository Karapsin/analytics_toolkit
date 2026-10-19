"""Real signed-task parsing and disposable coordinator authority; fake transport only."""

from __future__ import annotations

import copy
from typing import Any

from tests.repository.github_agent import controller


class PreparationCase:
    def __init__(self, tmp_path: Any, monkeypatch: Any):
        monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.setenv("AGENT_CONTROL_DIR", str(tmp_path))
        self.proto = controller.trusted_module("task_context")
        self.controls = controller.trusted_module("review_control")
        self.context = {
            "version": 1,
            "task_id": "readonly-preparation",
            "source": "user",
            "producer_id": "local:readonly-preparation",
            "starting_prompt": "Preserve the complete feature while reassessing its prerequisite.",
            "corrections": [],
        }
        self.pr = {
            "number": 1,
            "state": "open",
            "draft": False,
            "user": {"type": "User", "login": "owner"},
            "labels": [],
            "head": {"sha": "a" * 40, "ref": "codex/feature", "repo": {"full_name": "owner/repo"}},
            "base": {"ref": "dev", "repo": {"full_name": "owner/repo"}},
            "body": self.proto.render(self.context),
        }
        self.repository = "owner/repo"
        self.current_base = "e" * 40
        self.commands: list[Any] = []
        self.calls: list[Any] = []
        self.on_checks: Any = lambda: None
        monkeypatch.setattr(controller, "signed_head", lambda *args: True)
        monkeypatch.setattr(controller, "command", self.command)
        self.task = {
            "kind": "review",
            "number": 1,
            "head": "a" * 40,
            "base": self.current_base,
            "branch": "codex/feature",
            "coordinator_request_id": "request-1",
            "user_task": self.context,
            "task_digest": self.proto.digest(self.context),
            "producer_id": self.context["producer_id"],
        }
        self.binding = {
            "repository": self.repository,
            "number": 1,
            "request_id": "request-1",
            "revision": 7,
            "candidate_head": self.task["head"],
            "task_digest": self.task["task_digest"],
            "dev_base": "b" * 40,
        }
        self.controls.hold_coordinator(self, self.pr, self.binding)
        self.work = tmp_path / "work"
        self.control = tmp_path / "control"
        prompts = self.control / ".github/agent/prompts"
        prompts.mkdir(parents=True)
        (prompts / "review.md").write_text("Trusted review context")

    def pull(self, number: int) -> dict[str, Any]:
        assert number == 1
        return copy.deepcopy(self.pr)

    def base(self) -> str:
        return self.current_base

    def checks(self, head: str) -> list[Any]:
        self.on_checks()
        return []

    def pages(self, *args: Any) -> list[Any]:
        return []

    def api(self, path: str, method: str = "GET", data: Any = None) -> Any:
        self.calls.append((path, method, data))
        assert method == "GET", "Read-only preparation must never mutate GitHub"
        raise AssertionError("Unexpected API request: " + path)

    def command(self, *args: Any, **kwargs: Any) -> str:
        self.commands.append(args)
        assert args[:4] == ("git", "log", "-1", "--format=%B")
        return "Published task\n" + self.proto.trailers(self.context)

    def prepare(self, stage: str = "plan") -> None:
        controller.prepare_coordinator_readonly(
            self,
            self.task,
            self.work,
            self.control,
            binding=self.binding,
            stage=stage,
        )
