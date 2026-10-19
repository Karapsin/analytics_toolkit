"""Independent wire fixtures and disposable control state for checked renewal."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from typing import Any

import pytest

from tests.repository.github_agent import controller


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def durable(controls: Any) -> tuple[str, ...]:
    with controls.connect() as db:
        return tuple(db.iterdump())


class Case:
    """Public-facade fixture; no GitHub operation is provided."""

    def __init__(self, tmp_path: Any, monkeypatch: Any, count: int = 2):
        monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
        self.controls = controller.trusted_module("review_control")
        self.gh = SimpleNamespace(repository="owner/repo")
        self.pr = {"number": 1, "state": "open", "head": {"sha": "1" * 40, "ref": "own"}}
        self.proof = "f" * 64
        self.scope = self.make_scope(count)
        with self.controls.connect():
            pass
        self.controls.hold_coordinator(self.gh, self.pr, self.scope["binding"])
        self.refresh()

    def make_scope(self, count: int) -> dict[str, Any]:
        plan = {
            "request_id": "request-1",
            "head": "a" * 40,
            "base": "b" * 40,
            "context_digest": "c" * 64,
            "plan_sha256": "d" * 64,
        }
        items = [
            {
                "kind": "pr_dev_merge",
                "repository": "owner/repo",
                "number": n,
                "target": "dev",
                "task_id": "task-" + str(n),
                "branch": "peer/" + str(n),
                "anchor_head": "a" * 40,
                "anchor_task_digest": "a" * 64,
                "required_before": ["code", "publication"],
                "reason": "Required predecessor",
                "plan_location": "approved plan",
            }
            for n in range(2, count + 2)
        ]
        identity = {
            "id": "e" * 32,
            "request_id": "request-1",
            "plan_binding": plan,
            "planner_thread_id": "planner",
        }
        receipt = {
            "version": 2,
            "binding": plan,
            "prerequisites": items,
            "profile_sources": {},
            "reassessments": [],
            "review_revision": 1,
            "execution": {
                "id": "e" * 32,
                "identity": identity,
                "thread_id": "reviewer",
                "events_sha256": "e" * 64,
                "result_sha256": "f" * 64,
            },
        }
        receipt["sha256"] = digest(receipt)
        binding = {
            "request_id": "request-1",
            "revision": 7,
            "repository": "owner/repo",
            "stage": "prerequisite-check",
            "dev_base": "b" * 40,
            "state_sha256": "c" * 64,
            "checkpoint_sha256": "d" * 64,
            "number": 1,
            "candidate_head": "1" * 40,
            "task_digest": "1" * 64,
            "kind": "pr_dev_merge",
            "prerequisite_review_digest": digest(receipt),
            "plan_binding_digest": digest(plan),
        }
        return {
            "version": 1,
            "repository": "owner/repo",
            "own": {"number": 1, "head": "1" * 40, "task_digest": "1" * 64, "branch": "own"},
            "binding": binding,
            "review_receipt": receipt,
            "peers": [
                {
                    "number": n,
                    "head": format(n, "040x"),
                    "task_digest": format(n, "064x"),
                    "branch": "peer/" + str(n),
                }
                for n in range(2, count + 2)
            ],
        }

    def refresh(self) -> None:
        self.expected = self.controls.prerequisite_authority_snapshot(
            self.gh.repository, self.scope
        )
        self.caller = {
            "version": 1,
            "operation": "checked-coordinator-renewal-v1",
            "repository": self.gh.repository,
            "number": 1,
            "request_id": "request-1",
            "hold_revision": 7,
            "request_revision": 8,
            "request_status": "waiting",
            "request_stage": "prerequisite-check",
            "generation": "a" * 32,
            "candidate_head": "1" * 40,
            "task_digest": "1" * 64,
            "dev_base": "b" * 40,
            "main_head": "c" * 40,
            "plan_receipt_sha256": self.scope["binding"]["prerequisite_review_digest"],
            "scope_sha256": digest(self.scope),
            "proof_sha256": self.proof,
            "checkpoint_sha256": "d" * 64,
        }
        for name in (
            "request_row",
            "patch",
            "worker_source",
            "controller_source",
            "runtime",
            "global_settings",
            "external_observation",
        ):
            self.caller[name + "_sha256"] = "a" * 64

    def renew(self, **changes: Any) -> dict[str, Any]:
        arguments = {
            "scope": self.scope,
            "expected_authority": self.expected,
            "proof_sha256": self.proof,
            "caller_binding": self.caller,
            **changes,
        }
        return self.controls.renew_coordinator_checked(
            self.gh, self.pr, self.scope["binding"], self.scope["own"]["task_digest"], **arguments
        )

    def execute(self, sql: str, parameters: tuple[Any, ...] = ()) -> None:
        with self.controls.connect() as db:
            db.execute(sql, parameters)

    def pending(self, code: str) -> Any:
        return pytest.raises(self.controls.CoordinatorAuthorityPending, match="^" + code + "$")
