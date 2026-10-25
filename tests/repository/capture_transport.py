from __future__ import annotations

import copy
import json

import pytest

from tests.repository._support.capture_profile import (
    HEAD,
    NONCE,
    D,
    Opener,
    R,
    Response,
    T,
    action_binding,
    envelope,
    event,
    post,
    private_root,
)


@pytest.fixture(autouse=True)
def credential(monkeypatch, tmp_path):
    monkeypatch.setenv("GH_TOKEN", "synthetic-test-credential")
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("GITHUB_REPOSITORY", T.REPOSITORY)


@pytest.mark.parametrize("status", [401, 403, 404, 422])
def test_known_refusals_retain_actual_status_and_body(tmp_path, status):
    messages = {
        401: "Bad credentials",
        403: "Resource not accessible by integration",
        404: "Not Found",
        422: "Validation Failed",
    }
    body = D.canonical({"message": messages[status]})
    opener = Opener(Response(status, body))
    with D.Store(private_root(tmp_path)) as store:
        journal = T.Journal(store, T.HTTP(opener=opener))
        receipt = post(journal)
        assert receipt["state"] == "rejected"
        assert receipt["http_status"] == status
        assert receipt["workflow_run_id"] is None
        assert store.read(("capture-transport", NONCE, "d" * 32, "body.bin")) == body
        assert post(journal) == receipt
        assert len(opener.requests) == 1


@pytest.mark.parametrize("status", [408, 500, 502, 503, 504])
def test_remote_effect_then_error_is_uncertain_and_never_reposted(tmp_path, status):
    remote_effects = []
    response = Response(status, D.canonical({"message": "Server Error"}))

    class AppliedOpener(Opener):
        def open(self, request, timeout):
            remote_effects.append("original-run-123")
            return super().open(request, timeout)

    opener = AppliedOpener(response)
    with D.Store(private_root(tmp_path)) as store:
        journal = T.Journal(store, T.HTTP(opener=opener))
        receipt = post(journal)
        original = D.canonical(receipt)
        assert receipt["http_status"] == status
        assert receipt["state"] == "uncertain"
        empty = journal.reconcile(receipt, "workflow-dispatch", [], complete=True)
        assert empty["disposition"] == "not_found_in_complete_window"
        assert post(T.Journal(store, T.HTTP(opener=opener))) == receipt
        with pytest.raises(T.D.Pending):
            post(journal, attempt="e" * 32)
        assert D.canonical(receipt) == original
    assert remote_effects == ["original-run-123"]


@pytest.mark.parametrize(("body", "declared"), [(b"{", 1), (b"{}", 100), (b"x" * 65537, 65537)])
def test_success_headers_with_unusable_body_remain_truthful_uncertainty(tmp_path, body, declared):
    opener = Opener(Response(200, body, declared=declared))
    with D.Store(private_root(tmp_path)) as store:
        receipt = post(T.Journal(store, T.HTTP(opener=opener)), action_binding(details=True))
        assert receipt["http_status"] == 200
        assert receipt["state"] == "uncertain"
        assert receipt["workflow_run_id"] is None
        facts = receipt["response_facts"]
        assert facts["captured_body_bytes"] == min(65536, len(body))
        if declared != len(body) or len(body) > 65536:
            assert facts["body_complete"] is False


def test_dispatch_200_and_204_are_endpoint_mode_specific(tmp_path):
    details = D.canonical(
        {
            "workflow_run_id": 123,
            "run_url": "https://api.github.com/repos/" + T.REPOSITORY + "/actions/runs/123",
            "html_url": "https://github.com/" + T.REPOSITORY + "/actions/runs/123",
        }
    )
    with D.Store(private_root(tmp_path)) as store:
        receipt = post(
            T.Journal(store, T.HTTP(opener=Opener(Response(200, details)))),
            action_binding(details=True),
        )
        assert receipt["state"] == "accepted"
        assert receipt["workflow_run_id"] == 123
        other = post(T.Journal(store, T.HTTP(opener=Opener(Response(204)))), intent="f" * 32)
        assert other["state"] == "accepted"
        assert other["workflow_run_id"] is None
        assert other["response_facts"]["body_sha256"] == D.digest(b"")
        bad = post(
            T.Journal(store, T.HTTP(opener=Opener(Response(204)))),
            action_binding(details=True),
            intent="1" * 32,
        )
        assert bad["state"] == "uncertain"
        assert bad["http_status"] == 204


@pytest.mark.parametrize("before", [True, False])
def test_timeout_preserves_whether_actual_status_was_received(tmp_path, before):
    def fail():
        raise TimeoutError

    opener = Opener(TimeoutError() if before else Response(201, b"", on_read=fail))
    with D.Store(private_root(tmp_path)) as store:
        receipt = post(T.Journal(store, T.HTTP(opener=opener)))
        assert receipt["http_status"] == (None if before else 201)
        assert receipt["state"] == "uncertain"
        assert receipt["response_facts"]["transport_error"] == (
            "timeout_before_status" if before else "timeout_after_status"
        )


def test_read1_slow_trickle_observes_absolute_deadline(tmp_path):
    times = [0.0]

    def trickle():
        times[0] += 4

    response = Response(201, declared=100, chunks=[b"x"] * 100, on_read=trickle)
    with D.Store(private_root(tmp_path)) as store:
        receipt = post(T.Journal(store, T.HTTP(clock=lambda: times[0], opener=Opener(response))))
        assert receipt["http_status"] == 201
        assert receipt["state"] == "uncertain"
        assert receipt["response_facts"]["captured_body_bytes"] == 5
        assert times[0] == 20
        assert response.closed


def test_posted_comment_and_found_after_500_keep_distinct_immutable_records(tmp_path):
    value = envelope()
    synthetic = event(value)
    binding = {
        "purpose": value["purpose"],
        "operation": value["operation"],
        "nonce": NONCE,
        "envelope_sha256": D.digest(D.canonical(value)),
        "admission_receipt_sha256": "b" * 64,
        "monitor_issue": 12,
        "comment_body_sha256": D.digest(R.monitor_body(value).encode()),
    }
    with D.Store(private_root(tmp_path)) as store:
        journal = T.Journal(store, T.HTTP(opener=Opener(Response(500, b"{}"))))
        receipt = journal.mutate(
            "monitor-comment",
            "issues/12/comments",
            {"body": R.monitor_body(value)},
            binding,
            intent_id=NONCE,
            attempt_id="d" * 32,
        )
        found = journal.reconcile(
            receipt, "monitor-comment", [synthetic["comment"]], verified_identity=77, complete=True
        )
        assert found["disposition"] == "matched"
        assert found["verified_identity"]["monitor_comment_id"] == 77
        assert receipt["http_status"] == 500
        assert receipt["state"] == "uncertain"
        positive = T.Journal(
            store, T.HTTP(opener=Opener(Response(201, D.canonical(synthetic["comment"]))))
        )
        receipt2 = positive.mutate(
            "monitor-comment",
            "issues/12/comments",
            {"body": R.monitor_body(value)},
            binding,
            intent_id="e" * 32,
            attempt_id="f" * 32,
        )
        assert receipt2["state"] == "posted"
        assert receipt2["monitor_comment_id"] == 77


@pytest.mark.parametrize("mutation", ["status", "state", "id", "complete", "bool", "mode", "extra"])
def test_closed_receipt_rejects_incoherent_observations(tmp_path, mutation):
    with D.Store(private_root(tmp_path)) as store:
        value = copy.deepcopy(post(T.Journal(store, T.HTTP(opener=Opener(Response(204))))))
    if mutation == "status":
        value["http_status"] = 401
    elif mutation == "state":
        value["state"] = "posted"
    elif mutation == "id":
        value["workflow_run_id"] = 123
    elif mutation == "complete":
        value["response_facts"]["observed_body_bytes"] = 10
    elif mutation == "bool":
        value["bridge_run_attempt"] = True
    elif mutation == "mode":
        value["return_run_details"] = True
    else:
        value["arbitrary"] = 1
    with pytest.raises(ValueError, match=r"capture|Capture|dispatch|response|Unsupported"):
        T.validate_receipt(value)


def test_credentials_fixed_paths_and_redirect_never_leak_or_retry(monkeypatch):
    opener = Opener(Response(302, b"{}"))
    http = T.HTTP(opener=opener)
    status, body, facts = http.request("issues/12/comments", "POST", {"body": "safe"})
    assert status == 302
    assert facts["transport_error"] == "redirect_refused"
    assert len(opener.requests) == 1
    request, _ = opener.requests[0]
    assert b"synthetic-test-credential" not in request.data
    assert "synthetic-test-credential" not in json.dumps(facts) + body.decode()
    with pytest.raises(ValueError, match=r"capture|Capture|dispatch|response|Unsupported"):
        http.request("https://untrusted.test", "POST", {})
    monkeypatch.delenv("GH_TOKEN")
    with pytest.raises(T.D.Pending):
        http.request("issues/12/comments", "POST", {})


@pytest.mark.parametrize("valid_guard", [True, False])
def test_after_remote_effect_discovers_exact_run_with_immutable_source_and_preflight(
    tmp_path, valid_guard
):
    binding = action_binding()
    run = {
        "id": 123,
        "path": T.WORKFLOW,
        "workflow_id": 88,
        "head_sha": HEAD,
        "head_branch": binding["source_tag"],
        "event": "workflow_dispatch",
        "run_attempt": 1,
        "repository": {"full_name": T.REPOSITORY},
        "display_title": "capture-receiver-v2 receiver-rehearsal 77 " + NONCE,
    }
    jobs = {
        "total_count": 1,
        "jobs": [
            {
                "name": "capture",
                "run_id": 123,
                "steps": [
                    {
                        "name": "Authenticate source and purpose before candidate execution",
                        "status": "completed",
                        "conclusion": "success" if valid_guard else "failure",
                    }
                ],
            }
        ],
    }

    class Platform(Opener):
        def open(self, request, timeout):
            self.requests.append((request, timeout))
            if request.method == "POST":
                return Response(503, b'{"message":"Server Error"}')
            value = jobs if "/attempts/1/jobs?" in request.full_url else {"workflow_runs": [run]}
            return Response(200, D.canonical(value))

    platform = Platform(None)
    with D.Store(private_root(tmp_path)) as store:
        journal = T.Journal(store, T.HTTP(opener=platform))
        receipt = post(journal)
        resolution = journal.discover(receipt)
        # Neither a matching title nor the named successful step proves the
        # exact authenticated envelope/originals and post-intent interval.
        assert resolution["disposition"] == "incomplete"
        assert resolution["verified_identity"] is None
        assert post(journal) == receipt
        assert receipt["state"] == "uncertain"
        assert receipt["http_status"] == 503
        assert sum(request.method == "POST" for request, _ in platform.requests) == 1


def test_invalid_mutation_binding_is_rejected_before_http_or_started_marker(tmp_path):
    opener = Opener(Response(204))
    with D.Store(private_root(tmp_path)) as store:
        with pytest.raises(ValueError, match=r"capture|Capture|dispatch|response|Unsupported"):
            post(
                T.Journal(store, T.HTTP(opener=opener)),
                dict(action_binding(), bridge_run_attempt=True),
            )
        assert not opener.requests
        with pytest.raises(FileNotFoundError):
            store.read(("capture-transport", NONCE, "d" * 32, "started.json"))
