"""Honest synthetic metadata/platform fakes, separate from actual App sample."""

from __future__ import annotations

import copy
import importlib.util
import json
from email.message import Message

from tests._support.paths import REPO_ROOT


def load(name):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / f".github/agent/{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


R = load("capture_receiver")
T = R.T
D = R.D
HEAD = "a" * 40
SHA = "b" * 64
NONCE = "c" * 32
FIXTURE = REPO_ROOT / "tests/repository/_support/fixtures/capture_receiver_v2/approved45.json"


def envelope(purpose="receiver-rehearsal"):
    auth = {
        "kind": "receiver-rehearsal-admission-v1",
        "operation_id": "d" * 32,
        "operation_revision": 0,
        "operation_generation": "e" * 32,
        "operation_task_digest": SHA,
        "operation_plan_sha256": SHA,
        "independent_operation_review_sha256": SHA,
        "operation_receipt_sha256": SHA,
        "admission_receipt_sha256": SHA,
        "fixture_id": "capture-receiver-sentinel-v1",
        "fixture_source_commit": HEAD,
        "fixture_manifest_sha256": SHA,
        "fixture_program_sha256": SHA,
        "case_id": "positive-fixed-source",
        "recipe_sha256": SHA,
    }
    if purpose == "production":
        auth = {
            "kind": "production-capture-admission-v1",
            "pr_number": 39,
            "request_id": "request-39",
            "revision": 0,
            "generation": "e" * 32,
            "task_digest": SHA,
            "plan_receipt_sha256": SHA,
            "patch_sha256": SHA,
            "candidate": "f" * 40,
            "candidate_base": "1" * 40,
            "gate_proof_sha256": SHA,
            "admission_receipt_sha256": SHA,
        }
    return {
        "schema_version": 2,
        "purpose": purpose,
        "operation": T.OPERATIONS[purpose],
        "repository": T.REPOSITORY,
        "source": {
            "profile_id": "capture_isolation_active_v1",
            "source_set_sha256": SHA,
            "expected_trusted_head": HEAD,
            "source_tag": "agent-visual-source-v1-" + HEAD,
            "source_deployment_receipt_sha256": SHA,
        },
        "attempt": {"nonce": NONCE, "created_at": 100, "expires_at": 400},
        "authorization": auth,
    }


def event(value):
    """Explicit synthetic nonPR positive; actual sample lives separately."""
    comment = copy.deepcopy(json.loads(FIXTURE.read_text())["actual_PR45_App_comment"])
    comment.update(
        id=77,
        issue_url="https://api.github.com/repos/" + T.REPOSITORY + "/issues/12",
        url="https://api.github.com/repos/" + T.REPOSITORY + "/issues/comments/77",
        body=R.monitor_body(value),
    )
    return {
        "repository": {"full_name": T.REPOSITORY},
        "sender": comment["user"],
        "comment": comment,
        "issue": {
            "id": 123,
            "number": 12,
            "state": "open",
            "labels": [{"name": "agent:state"}],
            "title": "GitHub agent integration monitor",
            "url": "https://api.github.com/repos/" + T.REPOSITORY + "/issues/12",
        },
    }


def environment(value):
    return {
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REPOSITORY": T.REPOSITORY,
        "INPUT_PROTOCOL": R.PROTOCOL,
        "INPUT_PURPOSE": value["purpose"],
        "INPUT_ENVELOPE": D.canonical(value).decode(),
        "INPUT_EXPECTED_TRUSTED_HEAD": HEAD,
        "INPUT_MONITOR_COMMENT_ID": "77",
        "GITHUB_SHA": HEAD,
        "GITHUB_REF": "refs/tags/agent-visual-source-v1-" + HEAD,
        "GITHUB_RUN_ID": "99",
        "GITHUB_RUN_ATTEMPT": "1",
    }


def action_binding(value=None, details=False):
    value = envelope() if value is None else value
    return {
        "purpose": value["purpose"],
        "operation": value["operation"],
        "nonce": NONCE,
        "envelope_sha256": D.digest(D.canonical(value)),
        "monitor_comment_id": 77,
        "workflow_id": 88,
        "workflow_path": T.WORKFLOW,
        "source_tag": value["source"]["source_tag"],
        "expected_trusted_head": HEAD,
        "bridge_run_id": 99,
        "bridge_run_attempt": 1,
        "return_run_details": details,
    }


class Response:
    def __init__(self, status, body=b"", *, declared=None, on_read=None, chunks=None):
        self.status = status
        self.body = body
        self.headers = Message()
        self.headers["Content-Type"] = "application/json"
        self.headers["Content-Length"] = str(len(body) if declared is None else declared)
        self.on_read = on_read
        self.chunks = None if chunks is None else iter(chunks)
        self.closed = False

    def read1(self, amount):
        if self.on_read:
            self.on_read()
        if self.chunks is not None:
            return next(self.chunks, b"")
        part, self.body = self.body[:amount], self.body[amount:]
        return part

    def close(self):
        self.closed = True


class Opener:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def private_root(tmp_path):
    path = tmp_path / "private"
    path.mkdir(mode=0o700)
    return path


def post(journal, binding=None, *, intent=NONCE, attempt="d" * 32):
    binding = action_binding() if binding is None else binding
    value = envelope(binding["purpose"])
    inputs = {
        "protocol": R.PROTOCOL,
        "purpose": binding["purpose"],
        "expected_trusted_head": binding["expected_trusted_head"],
        "monitor_comment_id": str(binding["monitor_comment_id"]),
        "envelope": D.canonical(value).decode(),
    }
    return journal.mutate(
        "workflow-dispatch",
        "actions/workflows/agent-visual-profile.yml/dispatches",
        {
            "ref": "agent-visual-source-v1-" + HEAD,
            "return_run_details": binding["return_run_details"],
            "inputs": inputs,
        },
        binding,
        intent_id=intent,
        attempt_id=attempt,
    )


def source_reference():
    prefix = "https://api.github.com/repos/" + T.REPOSITORY + "/git/"
    return {
        "ref": "refs/tags/agent-visual-source-v1-" + HEAD,
        "url": prefix + "refs/tags/agent-visual-source-v1-" + HEAD,
        "object": {"type": "commit", "sha": HEAD, "url": prefix + "commits/" + HEAD},
    }


def monitor_post(journal):
    value = envelope()
    body = R.monitor_body(value)
    binding = {
        "purpose": value["purpose"],
        "operation": value["operation"],
        "nonce": NONCE,
        "envelope_sha256": D.digest(D.canonical(value)),
        "admission_receipt_sha256": SHA,
        "monitor_issue": 12,
        "comment_body_sha256": D.digest(body.encode()),
    }
    return journal.mutate(
        "monitor-comment",
        "issues/12/comments",
        {"body": body},
        binding,
        intent_id=NONCE,
        attempt_id="d" * 32,
    )


class IdentityPlatform(Opener):
    """One actual transport POST followed by separately mutable GET fixtures."""

    def __init__(self, response_value, *, endpoint):
        super().__init__(None)
        self.response_value = self.get_value = response_value
        self.endpoint = endpoint

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        if request.method == "POST":
            return Response(201, D.canonical(self.response_value))
        value = self.get_value
        if self.endpoint == "monitor-comment" and "/comments?" in request.full_url:
            value = [value]
        return Response(200, D.canonical(value))
