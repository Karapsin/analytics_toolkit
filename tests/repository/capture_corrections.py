"""Real protected seams rejected by the first independent whole-source review."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.repository._support.capture_native import source_checkout
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
    load,
    post,
    private_root,
)


@pytest.mark.parametrize("location", ["body", "header"])
def test_after_effect_secret_observation_is_private_and_public_delivery_pending(
    tmp_path, monkeypatch, location
):
    token = "ghs_SyntheticCredentialOnly"
    monkeypatch.setenv("GH_TOKEN", token)
    private = tmp_path / "quarantine"
    private.mkdir(mode=0o700)
    original_body = token.encode() if location == "body" else b"remote effect"
    response = Response(503, original_body)
    if location == "header":
        response.headers.replace_header("Content-Type", token)
    opener = Opener(response)
    with D.Store(private_root(tmp_path)) as store:
        journal = T.Journal(store, T.HTTP(opener=opener, private_root=private))
        with pytest.raises(T.PrivateObservationPending) as raised:
            post(journal)
        reference = D.parse(
            store.read(("capture-transport", NONCE, "d" * 32, "delivery-pending.json"))
        )
        assert reference["http_status"] == 503
        assert reference["body_sha256"] == D.digest(original_body)
        assert reference["delivery"] == "pending-private-only"
        assert token not in str(raised.value)
        public = b"".join(
            path.read_bytes() for path in (tmp_path / "private").rglob("*") if path.is_file()
        )
        assert token.encode() not in public
        with pytest.raises(D.Pending, match="Unresolved"):
            post(journal)
        assert len(opener.requests) == 1
    observations = list(private.glob("*/observation.json"))
    assert len(observations) == 1
    original = D.parse(observations[0].read_bytes())
    assert original["http_status"] == 503
    assert (observations[0].parent / "body.bin").read_bytes() == original_body
    if location == "header":
        assert original["response_facts"]["safe_headers"]["content_type"] == token


@pytest.mark.parametrize("bad", [False, True, 0.0, 1.0])
def test_nested_body_count_requires_integer_even_when_python_equal(tmp_path, monkeypatch, bad):
    monkeypatch.setenv("GH_TOKEN", "synthetic-token")
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    body = b"x" if bad in (True, 1.0) else b""
    with D.Store(private_root(tmp_path)) as store:
        value = post(T.Journal(store, T.HTTP(opener=Opener(Response(503, body)))))
    value["response_facts"]["body_ref"]["bytes"] = bad
    with pytest.raises(ValueError, match="binding"):
        T.validate_receipt(value)


@pytest.mark.parametrize("name", ["Retry-After", "X-RateLimit-Remaining"])
def test_wire_header_integer_upper_boundary(name):
    assert T._safe_int({name: "2147483647"}, name, 2147483647) == 2147483647
    with pytest.raises(ValueError, match="bounded"):
        T._safe_int({name: "2147483648"}, name, 2147483647)


@pytest.mark.parametrize("mode", ["unlabelled", "duplicate", "changed", "partial"])
def test_actual_preflight_requires_unique_complete_enrolled_monitor(tmp_path, mode):
    receiver, _value, env, api = source_checkout(tmp_path)
    original = api.request

    def request(path):
        status, raw, facts = original(path)
        if path.startswith("issues?state="):
            rows = D.parse(raw)
            if mode == "unlabelled":
                rows[0]["labels"] = []
            elif mode == "duplicate":
                rows = rows * 2
            elif mode == "changed":
                rows[0]["id"] += 1
            else:
                rows = rows * 100
            raw = D.canonical(rows)
        return status, raw, facts

    with pytest.raises((ValueError, receiver.D.Pending)):
        receiver.preflight(env, SimpleNamespace(request=request), now=100)


def test_fresh_bridge_stores_and_incomplete_prior_history_never_post(tmp_path):
    value = envelope()
    synthetic = event(value)
    calls = []

    class API:
        def request(self, path, method="GET", payload=None):
            calls.append((path, method))
            assert method == "GET", "Missing durable prior history cannot authorize POST"
            if path == "git/ref/heads/main":
                result = {"ref": "refs/heads/main", "object": {"type": "commit", "sha": HEAD}}
            elif path.startswith("git/ref/tags/"):
                result = {
                    "ref": "refs/tags/" + value["source"]["source_tag"],
                    "object": {"type": "commit", "sha": HEAD},
                }
            elif path.startswith("issues?state="):
                result = [synthetic["issue"]]
            elif path.endswith("/runs?event=workflow_dispatch&per_page=100&page=1"):
                # An earlier applied dispatch with lost delivery cannot be erased
                # by this fresh Store/run or by an empty later navigation window.
                return 503, b"partial earlier history", {"body_complete": False}
            else:
                result = {
                    "issues/comments/77": synthetic["comment"],
                    "issues/12": synthetic["issue"],
                    "actions/workflows/agent-visual-profile.yml": {
                        "id": 88,
                        "path": T.WORKFLOW,
                        "state": "active",
                    },
                }[path]
            return 200, D.canonical(result), {"body_complete": True}

    for run in (99, 100):
        root = tmp_path / str(run)
        root.mkdir(mode=0o700)
        with D.Store(root) as store, pytest.raises(D.Pending):
            R.dispatch_from_event(
                synthetic,
                http=API(),
                store=store,
                environment={
                    "GITHUB_REPOSITORY": T.REPOSITORY,
                    "GITHUB_SHA": HEAD,
                    "GITHUB_RUN_ATTEMPT": "1",
                    "GITHUB_RUN_ID": str(run),
                },
                now=100,
            )
        assert (root / "bridge-navigation.json").exists()
    assert all(method == "GET" for _, method in calls)


def test_joint_budget_combines_source_and_immutable_generated_members(tmp_path):
    receiver, _value, _env, api = source_checkout(tmp_path)
    budget = receiver.D.joint(api)
    receiver.source_inventory(
        tmp_path / "control",
        receiver.subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=tmp_path / "control", capture_output=True, check=True
        )
        .stdout.decode()
        .strip(),
        budget,
    )
    before = budget.used
    assert before > 0
    # Each member is below its individual cap; only their common aggregate fails.
    budget.limit = before + 65536 + 2500
    root = tmp_path / "joint"
    root.mkdir(mode=0o700)
    with receiver.D.Store(root, budget=budget) as store:
        store.put(("first.bin",), b"a" * 1000)
        with pytest.raises(receiver.D.BudgetPending):
            store.put(("second.bin",), b"b" * 1000)
    assert (root / "first.bin").read_bytes() == b"a" * 1000
    assert budget.used <= budget.limit


@pytest.mark.parametrize("head", [None, "0" * 40])
def test_prepared_wrong_or_missing_parent_refused_before_live_boundary(monkeypatch, head):
    controller = load("controller")
    value = envelope("production")
    auth = value["authorization"]
    prepared = {
        "candidate": auth["candidate"],
        "head": head,
        "base": HEAD,
        "tree": HEAD,
        "patch_sha256": auth["patch_sha256"],
        "task_digest": auth["task_digest"],
    }
    if head is None:
        del prepared["head"]
    monkeypatch.setattr(controller.time, "time", lambda: 100)
    with pytest.raises(ValueError, match="candidate/admission"):
        controller.publish_profile_visual_candidate(
            SimpleNamespace(repository=T.REPOSITORY), prepared, value, {}
        )


def test_named_step_and_run_title_cannot_authenticate_wrong_originals_or_time():
    receipt = action_binding()
    identity = {
        "kind": "capture-profile-run-discovery-v1",
        "run": {
            "id": 123,
            "path": T.WORKFLOW,
            "workflow_id": 88,
            "head_sha": HEAD,
            "head_branch": receipt["source_tag"],
            "event": "workflow_dispatch",
            "run_attempt": 1,
            "repository": {"full_name": T.REPOSITORY},
            "display_title": "capture-receiver-v2 receiver-rehearsal 77 " + NONCE,
        },
        "jobs": {
            "total_count": 1,
            "jobs": [
                {
                    "name": "capture",
                    "run_id": 123,
                    "steps": [
                        {
                            "name": "Authenticate source and purpose before candidate execution",
                            "status": "completed",
                            "conclusion": "success",
                        }
                    ],
                }
            ],
        },
    }
    for altered in ({"envelope_sha256": "0" * 64}, {"created_at": "1970-01-01T00:00:00Z"}):
        # These untrusted assertions alongside perfect navigation never supply
        # authenticated receiver originals or the durable post-intent interval.
        with pytest.raises(D.Pending, match="originals"):
            T.verified_run(dict(identity, **altered), receipt)


def test_private_retention_unavailable_refuses_before_remote_effect(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "synthetic-token")
    monkeypatch.delenv("RUNNER_TEMP", raising=False)
    opener = Opener(Response(204))
    with pytest.raises(D.Pending, match="before request"):
        T.HTTP(opener=opener).request(
            "actions/workflows/agent-visual-profile.yml/dispatches", "POST", {}
        )
    assert not opener.requests


class AbnormalResponse(Response):
    """After-effect backend faults; no real request or stream writer exists."""

    def __init__(self, body, read_failure, close_failure):
        super().__init__(503, body, declared=len(body) + (1 if read_failure else 0))
        self.read_failure = read_failure
        self.close_failure = close_failure
        self.read_count = 0

    def read1(self, amount):
        self.read_count += 1
        if self.read_count > 1 and self.read_failure is not None:
            raise self.read_failure
        return super().read1(amount)

    def close(self):
        super().close()
        if self.close_failure is not None:
            raise self.close_failure


@pytest.mark.parametrize(
    "failure", ["close-full", "close-partial", "interrupt", "exit", "runtime", "type"]
)
@pytest.mark.parametrize("secret_body", [False, True])
def test_abnormal_http_exit_retains_observed_prefix_status_before_safe_pending(
    tmp_path, monkeypatch, failure, secret_body
):
    token = "ghs_SyntheticAbnormalOnly"
    monkeypatch.setenv("GH_TOKEN", token)
    prefix = b"prefix:" + (token.encode() if secret_body else b"no-secret")
    read_failures = {
        "close-partial": TimeoutError(token),
        "interrupt": KeyboardInterrupt(token),
        "exit": SystemExit(token),
        "runtime": RuntimeError(token),
        "type": TypeError(token),
    }
    response = AbnormalResponse(
        prefix, read_failures.get(failure), OSError(token) if failure.startswith("close") else None
    )
    quarantine = tmp_path / "quarantine"
    quarantine.mkdir(mode=0o700)
    opener = Opener(response)
    ledger = private_root(tmp_path)
    with D.Store(ledger) as store:
        journal = T.Journal(store, T.HTTP(opener=opener, private_root=quarantine))
        with pytest.raises(T.PrivateObservationPending) as raised:
            post(journal)
        reference = raised.value.reference
        assert reference["http_status"] == 503
        assert reference["captured_body_bytes"] == reference["observed_body_bytes"] == len(prefix)
        assert reference["body_sha256"] == D.digest(prefix)
        assert reference["private_retained"] is True
        assert reference["state"] == "pending"
        assert reference["body_complete"] is False
        assert token not in str(raised.value)
        with pytest.raises(FileNotFoundError):
            store.read(("capture-transport", NONCE, "d" * 32, "receipt.json"))
    # A new HTTP object/Store after the ambiguous effect cannot repost.
    with D.Store(ledger) as restarted, pytest.raises(D.Pending, match="Unresolved"):
        post(T.Journal(restarted, T.HTTP(opener=opener, private_root=quarantine)))
    assert len(opener.requests) == 1
    raw = (quarantine / reference["private_observation_sha256"] / "body.bin").read_bytes()
    observation = D.parse(
        (quarantine / reference["private_observation_sha256"] / "observation.json").read_bytes()
    )
    assert raw == prefix
    assert observation["http_status"] == 503
    assert observation["response_facts"]["transport_error"] in {"io_error", "invalid_http"}
    if failure == "close-partial":
        assert observation["before_close_error"] == "timeout_after_status"
        assert observation["close_failed"] is True
    public = b"".join(path.read_bytes() for path in ledger.rglob("*") if path.is_file())
    assert token.encode() not in public


def test_before_status_interruption_keeps_actual_null_and_empty_original(tmp_path, monkeypatch):
    token = "ghs_SyntheticPreStatusOnly"
    monkeypatch.setenv("GH_TOKEN", token)
    quarantine = tmp_path / "quarantine"
    quarantine.mkdir(mode=0o700)

    class InterruptedOpener:
        def __init__(self):
            self.effects = 0

        def open(self, _request, _timeout=None, **_kwargs):
            self.effects += 1
            raise KeyboardInterrupt(token)

    opener = InterruptedOpener()
    with D.Store(private_root(tmp_path)) as store:
        with pytest.raises(T.PrivateObservationPending) as raised:
            post(T.Journal(store, T.HTTP(opener=opener, private_root=quarantine)))
        reference = raised.value.reference
        assert reference["http_status"] is None
        assert reference["observed_body_bytes"] == reference["captured_body_bytes"] == 0
        assert reference["body_sha256"] == D.digest(b"")
        assert reference["private_retained"] is True
    assert opener.effects == 1
    assert (quarantine / reference["private_observation_sha256"] / "body.bin").read_bytes() == b""
    assert token not in str(raised.value)


def test_private_persistence_failure_reports_unavailable_without_acceptance(tmp_path, monkeypatch):
    token = "ghs_SyntheticStorageOnly"
    monkeypatch.setenv("GH_TOKEN", token)
    quarantine = tmp_path / "quarantine"
    quarantine.mkdir(mode=0o700)
    identity = (quarantine.stat().st_dev, quarantine.stat().st_ino)
    original_put = D.Store.put

    def put(store, *args, **kwargs):
        if store.root_identity == identity:
            raise OSError(token)
        return original_put(store, *args, **kwargs)

    monkeypatch.setattr(D.Store, "put", put)
    opener = Opener(Response(503, b"observed body"))
    with D.Store(private_root(tmp_path)) as store:
        with pytest.raises(T.PrivateObservationPending) as raised:
            post(T.Journal(store, T.HTTP(opener=opener, private_root=quarantine)))
        reference = raised.value.reference
        assert reference["http_status"] == 503
        assert reference["body_sha256"] == D.digest(b"observed body")
        assert reference["private_retained"] is False
        assert reference["delivery"] == "pending-unavailable"
        with pytest.raises(D.Pending, match="Unresolved"):
            post(T.Journal(store, T.HTTP(opener=opener, private_root=quarantine)))
    assert len(opener.requests) == 1
    assert token not in str(raised.value)
    assert not list(quarantine.iterdir())
