from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from tests.repository._support.capture_profile import (
    FIXTURE,
    HEAD,
    SHA,
    D,
    R,
    T,
    envelope,
    environment,
    event,
    load,
    private_root,
)


@pytest.mark.parametrize("purpose", ["production", "receiver-rehearsal"])
def test_exact_union_canonical_and_non_circular_admission_core(purpose):
    value = envelope(purpose)
    assert R.validate_envelope(D.canonical(value), expected_purpose=purpose, now=100) == value
    core = D.parse(R.binding_bytes(value))
    assert "admission_receipt_sha256" not in core["authorization"]
    assert (
        "gate_proof_sha256" in core["authorization"]
        if purpose == "production"
        else "operation_receipt_sha256" in core["authorization"]
    )
    with pytest.raises(ValueError, match=r"capture|Capture|rehearsal|Rehearsal|candidate"):
        R.validate_envelope(json.dumps(value).encode(), expected_purpose=purpose, now=100)
    with pytest.raises(ValueError, match=r"capture|Capture|rehearsal|Rehearsal|candidate"):
        R.validate_envelope(D.canonical(value), expected_purpose=purpose, now=401)


@pytest.mark.parametrize(
    "mutation", ["mixed", "extra", "bool", "old", "tag", "window", "fixture", "unknown"]
)
def test_unknown_mixed_and_incoherent_envelopes_fail_closed(mutation):
    value = envelope()
    if mutation == "mixed":
        value["authorization"] = envelope("production")["authorization"]
    elif mutation == "extra":
        value["authorization"]["candidate"] = HEAD
    elif mutation == "bool":
        value["authorization"]["operation_revision"] = False
    elif mutation == "old":
        value["schema_version"] = 1
    elif mutation == "tag":
        value["source"]["source_tag"] = "main"
    elif mutation == "window":
        value["attempt"]["expires_at"] = 401
    elif mutation == "fixture":
        value["authorization"]["fixture_source_commit"] = "f" * 40
    else:
        value["purpose"] = "capability-only"
    with pytest.raises(ValueError, match=r"capture|Capture|rehearsal|Rehearsal|candidate"):
        R.validate_envelope(D.canonical(value), expected_purpose="receiver-rehearsal", now=100)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"a":1,"a":2}',
        b'{"x":1e9999}',
        b'{"x":NaN}',
        b'"\xff"',
        b"[" * 65 + b"]" * 65,
        b'{"x":9007199254740992}',
    ],
)
def test_bounded_json_rejects_hostile_data_before_semantic_use(raw):
    with pytest.raises((ValueError, UnicodeError)):
        D.parse(raw)


def test_real_app_sample_is_not_a_non_pr_monitor_positive():
    value = envelope()
    actual = json.loads(FIXTURE.read_text())["actual_PR45_App_comment"]
    synthetic = event(value)
    assert R.authenticate_comment(synthetic, value) == 77
    altered = copy.deepcopy(synthetic)
    altered["comment"] = actual
    altered["issue"]["pull_request"] = {"url": "actual PR45"}
    with pytest.raises(ValueError, match=r"capture|Capture|rehearsal|Rehearsal|candidate"):
        R.authenticate_comment(altered, value)
    for field in ("user", "performed_via_github_app"):
        altered = copy.deepcopy(synthetic)
        altered["comment"][field]["id"] += 1
        with pytest.raises(ValueError, match=r"capture|Capture|rehearsal|Rehearsal|candidate"):
            R.authenticate_comment(altered, value)


@pytest.mark.parametrize("purpose", ["production", "receiver-rehearsal"])
def test_private_admission_parser_is_not_a_live_permission(tmp_path, purpose):
    value = envelope(purpose)
    auth = value["authorization"]
    if purpose == "production":
        receipt = {
            "schema_version": 1,
            "kind": auth["kind"],
            "purpose": purpose,
            **{k: auth[k] for k in ("request_id", "revision", "generation", "gate_proof_sha256")},
            "envelope_binding_sha256": D.digest(R.binding_bytes(value)),
            "authority_snapshot_sha256": SHA,
            "issued_at": 100,
            "expires_at": 400,
        }
        path = (
            "requests",
            auth["request_id"],
            "capture-admissions",
            value["attempt"]["nonce"],
            "admission.json",
        )
        reference = {
            "schema_version": 1,
            "purpose": purpose,
            "request_id": auth["request_id"],
            "nonce": value["attempt"]["nonce"],
        }
    else:
        receipt = {
            "schema_version": 1,
            "kind": auth["kind"],
            "purpose": purpose,
            **{
                k: auth[k]
                for k in (
                    "operation_id",
                    "operation_revision",
                    "operation_generation",
                    "operation_receipt_sha256",
                    "case_id",
                    "recipe_sha256",
                    "fixture_manifest_sha256",
                )
            },
            "envelope_binding_sha256": D.digest(R.binding_bytes(value)),
            "authority_snapshot_sha256": SHA,
            "issued_at": 100,
            "expires_at": 400,
        }
        path = (
            "source-deployments",
            auth["operation_id"],
            "admissions",
            auth["case_id"],
            value["attempt"]["nonce"] + ".json",
        )
        reference = {
            "schema_version": 1,
            "purpose": purpose,
            "operation_id": auth["operation_id"],
            "case_id": auth["case_id"],
            "nonce": value["attempt"]["nonce"],
        }
    # Synthetic parser fixture only, NOT a genuine issuer/Gate proof.
    raw = D.canonical(receipt)
    reference["receipt_sha256"] = D.digest(raw)
    value["authorization"]["admission_receipt_sha256"] = D.digest(raw)
    root = private_root(tmp_path)
    with D.Store(root) as store:
        store.put(path, raw)
    assert R.read_host_admission(root, reference, value, now=101) == receipt
    with pytest.raises(D.Pending, match=r"not implemented"):
        R.require_live_host_boundary(purpose)
    with pytest.raises(ValueError, match=r"capture|Capture|rehearsal|Rehearsal|candidate"):
        R.read_host_admission(root, dict(reference, path="arbitrary"), value, now=101)


@pytest.mark.parametrize(
    ("field", "newvalue"),
    [
        ("GITHUB_SHA", "f" * 40),
        ("GITHUB_REF", "refs/heads/main"),
        ("INPUT_PROTOCOL", "legacy"),
        ("INPUT_PURPOSE", "production"),
        ("GITHUB_RUN_ATTEMPT", "2"),
        ("INPUT_EXPECTED_TRUSTED_HEAD", "f" * 40),
    ],
)
def test_wrong_generation_source_or_purpose_never_calls_any_platform(field, newvalue):
    value = envelope()
    env = environment(value)
    env[field] = newvalue
    http = SimpleNamespace(
        request=lambda *args: pytest.fail("No candidate or sentinel/platform may run")
    )
    with pytest.raises(ValueError, match=r"capture|Capture|rehearsal|Rehearsal|candidate"):
        R.preflight(env, http, now=100)


def test_public_production_and_source_operations_are_pending_before_side_effects(monkeypatch):
    controller = load("controller")
    gh = SimpleNamespace(repository=T.REPOSITORY, api=lambda *a: pytest.fail("No GitHub mutation"))
    monkeypatch.setattr(controller.time, "time", lambda: 100)
    with pytest.raises(D.Pending):
        controller.dispatch_profile_request(gh, envelope("production"), {})
    with pytest.raises(ValueError, match=r"capture|Capture|rehearsal|Rehearsal|candidate"):
        controller.dispatch_profile_request(gh, envelope(), {})
    with pytest.raises(D.Pending):
        R.create_source_pin(gh, HEAD, {}, {})
    with pytest.raises(ValueError, match=r"capture|Capture|rehearsal|Rehearsal|candidate"):
        R.dispatch_rehearsal_request(gh, {"candidate": HEAD}, "positive-fixed-source", {})


def test_new_workflow_policy_is_exact_and_preserves_other_denials():
    controller = load("controller")
    assert controller.policy_path(T.WORKFLOW)
    for path in (
        T.WORKFLOW + ".bak",
        ".github/workflows/nested/agent-visual-profile.yml",
        ".github/workflows/agent-visual-profile2.yml",
        "analytics_toolkit/sql_explorer/app.py",
    ):
        assert not controller.policy_path(path)


@pytest.mark.parametrize(
    "fault", [None, "wrong-parent", "extra-parent", "no-parent", "head-change", "base-target"]
)
def test_candidate_base_is_feature_parent_and_not_the_distinct_reviewed_dev_base(fault):
    value = envelope("production")
    auth = value["authorization"]
    feature, dev = auth["candidate_base"], "2" * 40
    assert feature != dev
    commit = {"sha": auth["candidate"], "parents": [{"sha": feature}]}
    pr = {
        "number": auth["pr_number"],
        "state": "open",
        "head": {"sha": feature, "repo": {"full_name": T.REPOSITORY}},
        "base": {"ref": "dev", "sha": dev, "repo": {"full_name": T.REPOSITORY}},
    }
    ref = {
        "ref": "refs/heads/agent-visual/" + auth["candidate"],
        "object": {"type": "commit", "sha": auth["candidate"]},
    }
    if fault == "wrong-parent":
        commit["parents"] = [{"sha": dev}]
    elif fault == "extra-parent":
        commit["parents"].append({"sha": dev})
    elif fault == "no-parent":
        commit["parents"] = []
    elif fault == "head-change":
        pr["head"]["sha"] = dev
    elif fault == "base-target":
        pr["base"]["ref"] = "main"

    def request(path):
        record = {
            "git/commits/" + auth["candidate"]: commit,
            "git/ref/heads/agent-visual/" + auth["candidate"]: ref,
            "pulls/39": pr,
        }[path]
        return 200, D.canonical(record), {"body_complete": True}

    http = SimpleNamespace(request=request)
    if fault is None:
        R.guard_candidate(value, http, [])
        assert pr["base"]["sha"] == dev
    else:
        with pytest.raises(ValueError, match=r"capture|Capture|rehearsal|Rehearsal|candidate"):
            R.guard_candidate(value, http, [])
