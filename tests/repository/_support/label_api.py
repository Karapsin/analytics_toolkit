"""Shared simulated GitHub label and merge API for controller contracts."""

from __future__ import annotations

import copy
from typing import Any

from tests.repository.github_agent import checks, owned_pr


class GitHub:
    repository = "owner/repo"

    def __init__(self) -> None:
        self.pr = owned_pr()
        self.pr.update(mergeable=True, labels=[{"name": "agent:needs-user"}, {"name": "feature"}])
        self.comments: list[dict[str, Any]] = []
        self.mutations: list[tuple[str, str]] = []
        self.on_delete: Any = None
        self.fail_delete = False
        self.fast = checks()

    def pull(self, number: int) -> dict[str, Any]:
        return copy.deepcopy(self.pr)

    def pulls(self) -> list[dict[str, Any]]:
        return [self.pull(1)]

    def base(self) -> str:
        return "base"

    def checks(self, head: str) -> list[dict[str, Any]]:
        return self.fast

    def pages(self, path: str) -> list[dict[str, Any]]:
        if "/files?" in path:
            return [{"filename": ".github/agent/controller.py"}]
        return self.comments

    def comment(self, number: int, body: str) -> None:
        self.comments.append({"body": body, "user": {"type": "Bot", "login": "app"}})

    def api(self, path: str, method: str = "GET", data: Any = None) -> Any:
        path = path.split("?", 1)[0]
        if method != "GET":
            self.mutations.append((path, method))
        if path.endswith("/labels") and method == "PUT":
            if self.fail_delete:
                message = "Transient label API failure"
                raise RuntimeError(message)
            self.pr["labels"] = [{"name": name} for name in data["labels"]]
            if self.on_delete:
                self.on_delete()
        elif path.endswith("/labels") and method == "POST":
            names = {item["name"] for item in self.pr["labels"]}
            self.pr["labels"].extend({"name": name} for name in data["labels"] if name not in names)
        elif "/labels/" in path and method == "DELETE":
            if self.fail_delete:
                message = "Transient label API failure"
                raise RuntimeError(message)
            self.pr["labels"] = [
                item for item in self.pr["labels"] if item["name"] != path.rsplit("/", 1)[-1]
            ]
            if self.on_delete:
                self.on_delete()
        elif path.endswith("/merge") and method == "PUT":
            assert data["sha"] == self.pr["head"]["sha"]
            self.pr["state"] = "closed"
            return {"merged": True}
        return copy.deepcopy(self.pr["labels"])

    def owner_approval(self, head: str = "a" * 40, actor: str = "owner") -> None:
        self.comments.append(
            {"body": "/agent approve-policy " + head, "user": {"type": "User", "login": actor}}
        )
