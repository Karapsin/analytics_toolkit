"""Malformed successful endpoint bodies remain immutable uncertain evidence."""

from __future__ import annotations

import copy

import pytest

from tests.repository._support.capture_capacity import source_post
from tests.repository._support.capture_profile import (
    HEAD,
    NONCE,
    D,
    IdentityPlatform,
    T,
    action_binding,
    envelope,
    event,
    monitor_post,
    post,
    private_root,
    source_reference,
)


@pytest.fixture(autouse=True)
def credential(monkeypatch, tmp_path):
    monkeypatch.setenv("GH_TOKEN", "synthetic-identity-token")
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))


@pytest.mark.parametrize("bad", [None, [], "scalar", 0, True])
@pytest.mark.parametrize(
    "member", ["object", "user", "performed_via_github_app", "source-top", "comment-top"]
)
def test_malformed_nested_201_retains_uncertain_then_discovers_exact_after_restart(
    tmp_path, bad, member
):
    source = member in {"object", "source-top"}
    correct = source_reference() if source else event(envelope())["comment"]
    malformed = copy.deepcopy(correct)
    if member.endswith("-top"):
        malformed = bad
    else:
        malformed[member] = bad
    platform = IdentityPlatform(
        malformed, endpoint="source-ref-create" if source else "monitor-comment"
    )
    send = source_post if source else monitor_post
    ledger = private_root(tmp_path)
    raw = D.canonical(malformed)
    with D.Store(ledger) as store:
        receipt = send(T.Journal(store, T.HTTP(opener=platform)))
        assert receipt["state"] == "uncertain"
        assert receipt["http_status"] == 201
        assert receipt["response_facts"]["body_sha256"] == D.digest(raw)
        assert store.read(("capture-transport", NONCE, "d" * 32, "body.bin")) == raw
        if not source:
            assert receipt["monitor_comment_id"] is None
    with D.Store(ledger) as restarted:
        journal = T.Journal(restarted, T.HTTP(opener=platform))
        assert send(journal) == receipt
        incomplete = journal.discover(receipt)
        assert incomplete["disposition"] == "incomplete"
        assert incomplete["verified_identity"] is None
        platform.get_value = correct
        matched = journal.discover(receipt)
        assert matched["disposition"] == "matched"
        assert send(journal) == receipt
    assert sum(req.method == "POST" for req, _ in platform.requests) == 1


@pytest.mark.parametrize(
    ("member", "bad"),
    [
        ("id", True),
        ("id", 77.0),
        ("id", "77"),
        (
            "user",
            {"id": 340139075.0, "type": "Bot", "login": "analytics-toolkit-agent-717959[bot]"},
        ),
        ("performed_via_github_app", {"id": 5250620.0, "slug": "analytics-toolkit-agent-717959"}),
        ("url", True),
        ("url", []),
        ("issue_url", 12),
    ],
)
def test_wrong_scalar_comment_ids_and_urls_do_not_become_posted(tmp_path, member, bad):
    malformed = event(envelope())["comment"]
    malformed[member] = bad
    platform = IdentityPlatform(malformed, endpoint="monitor-comment")
    with D.Store(private_root(tmp_path)) as store:
        journal = T.Journal(store, T.HTTP(opener=platform))
        receipt = monitor_post(journal)
        assert receipt["state"] == "uncertain"
        assert receipt["http_status"] == 201
        assert receipt["monitor_comment_id"] is None
        assert receipt["response_facts"]["body_sha256"] == D.digest(D.canonical(malformed))
        assert journal.discover(receipt)["disposition"] == "incomplete"
        assert monitor_post(journal) == receipt
    assert sum(req.method == "POST" for req, _ in platform.requests) == 1


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        True,
        0,
        "https://api.github.com/repos/foreign/repo/git/refs/tags/x",
        "https://api.github.com/repos/Karapsin/analytics_toolkit/git/refs/tags/wrong",
    ],
)
@pytest.mark.parametrize("member", ["url", "object-url", "object-sha"])
def test_missing_wrong_or_foreign_reference_urls_are_uncertain(tmp_path, member, bad):
    malformed = source_reference()
    target = malformed if member == "url" else malformed["object"]
    key = "sha" if member == "object-sha" else "url"
    if bad is None and member == "url":
        del target["url"]
    else:
        target[key] = bad
    platform = IdentityPlatform(malformed, endpoint="source-ref-create")
    ledger = private_root(tmp_path)
    with D.Store(ledger) as store:
        receipt = source_post(T.Journal(store, T.HTTP(opener=platform)))
        assert receipt["state"] == "uncertain"
        assert receipt["http_status"] == 201
        assert receipt["response_facts"]["body_sha256"] == D.digest(D.canonical(malformed))
    with D.Store(ledger) as restarted:
        journal = T.Journal(restarted, T.HTTP(opener=platform))
        assert source_post(journal) == receipt
        assert journal.discover(receipt)["verified_identity"] is None
        platform.get_value = source_reference()
        assert journal.discover(receipt)["disposition"] == "matched"
        assert source_post(journal) == receipt
    assert sum(req.method == "POST" for req, _ in platform.requests) == 1


@pytest.mark.parametrize("source", [True, False])
def test_complete_fixed_identity_still_produces_successful_transport_receipt(tmp_path, source):
    value = source_reference() if source else event(envelope())["comment"]
    send = source_post if source else monitor_post
    platform = IdentityPlatform(
        value, endpoint="source-ref-create" if source else "monitor-comment"
    )
    with D.Store(private_root(tmp_path)) as store:
        journal = T.Journal(store, T.HTTP(opener=platform))
        receipt = send(journal)
        assert receipt["state"] == ("created_response" if source else "posted")
        assert receipt["http_status"] == 201
        assert journal.discover(receipt)["disposition"] == "matched"
        assert send(journal) == receipt
    assert sum(req.method == "POST" for req, _ in platform.requests) == 1


def test_ordinary_user_comment_with_null_app_remains_valid_nonmatching_navigation():
    value = event(envelope())["comment"]
    value["user"] = {"id": 99, "login": "synthetic-owner", "type": "User"}
    value["performed_via_github_app"] = None
    assert T.comment_metadata(value) is True
    assert (
        T.comment_identity(value, {"monitor_issue": 12, "comment_body_sha256": "b" * 64}) is False
    )


@pytest.mark.parametrize(
    ("member", "bad"),
    [
        ("repository", None),
        ("repository", []),
        ("repository", "scalar"),
        ("repository", True),
        ("page", None),
        ("page", []),
        ("page", True),
        ("workflow_id", 88.0),
    ],
)
def test_malformed_run_navigation_stays_incomplete_without_another_post(tmp_path, member, bad):
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
    run[member] = bad
    platform = IdentityPlatform({}, endpoint="workflow-dispatch")
    platform.get_value = bad if member == "page" else {"workflow_runs": [run]}
    with D.Store(private_root(tmp_path)) as store:
        journal = T.Journal(store, T.HTTP(opener=platform))
        receipt = post(journal)
        result = journal.discover(receipt)
        assert result["disposition"] == "incomplete"
        assert result["verified_identity"] is None
        assert post(journal) == receipt
    assert sum(req.method == "POST" for req, _ in platform.requests) == 1
