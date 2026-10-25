"""Actual HTTP+Journal request/response reservation at the shared byte limit."""

from __future__ import annotations

import pytest

from tests.repository._support.capture_capacity import (
    BoundaryHTTP,
    maximum_headers,
    original_observation,
    source_post,
)
from tests.repository._support.capture_profile import NONCE, D, Opener, Response, T, private_root
from tests.repository.capture_corrections import AbnormalResponse


@pytest.mark.parametrize(
    ("remaining", "credit", "expected_limit"),
    [(655368, False, 65519), (655500, False, 65536), (655499, False, 65535), (655368, True, 65536)],
)
def test_actual_nonempty_post_reserves_whole_response_before_remote_effect(
    tmp_path, monkeypatch, remaining, credit, expected_limit
):
    monkeypatch.setenv("GH_TOKEN", "synthetic-budget-token")
    quarantine = tmp_path / "quarantine"
    quarantine.mkdir(mode=0o700)
    body = b"x" * 65536
    opener = Opener(maximum_headers(Response(503, body)))
    http = BoundaryHTTP(remaining=remaining, opener=opener, private_root=quarantine)
    if credit:
        http.opener = Opener(Response(200))
        assert http.request("git/ref/heads/main")[0] == 200
        http.opener = opener
    ledger = private_root(tmp_path)
    with D.Store(ledger) as store:
        receipt = source_post(T.Journal(store, http))
        assert http.entries[0][2] == remaining
        assert (http.entries[0][1] > 0) is credit
        if not credit and remaining == 655368:
            assert http.entries[0] == (16056312, 0, 655368)
        assert receipt["state"] == "uncertain"
        assert receipt["http_status"] == 503
        facts = receipt["response_facts"]
        assert facts["captured_body_bytes"] == expected_limit
        assert facts["body_sha256"] == D.digest(body[:expected_limit])
        assert facts["body_complete"] is (expected_limit == len(body))
    with D.Store(ledger) as restarted:
        assert source_post(T.Journal(restarted, T.HTTP(opener=opener))) == receipt
    observation, retained = original_observation(quarantine)
    assert observation["accounting_complete"] is True
    assert retained == body[:expected_limit]
    assert len(opener.requests) == 1
    assert http.budget.used <= http.budget.limit


@pytest.mark.parametrize("failure", ["close", "interrupt"])
def test_near_limit_abnormal_effect_keeps_actual_prefix_and_status(tmp_path, monkeypatch, failure):
    token = "ghs_SyntheticBoundaryOnly"
    monkeypatch.setenv("GH_TOKEN", token)
    quarantine = tmp_path / "quarantine"
    quarantine.mkdir(mode=0o700)
    prefix = token.encode() + b"x" * 65000
    response = AbnormalResponse(
        prefix,
        KeyboardInterrupt(token) if failure == "interrupt" else None,
        OSError(token) if failure == "close" else None,
    )
    opener = Opener(maximum_headers(response))
    http = BoundaryHTTP(remaining=655368, opener=opener, private_root=quarantine)
    ledger = private_root(tmp_path)
    with D.Store(ledger) as store, pytest.raises(T.PrivateObservationPending) as raised:
        source_post(T.Journal(store, http))
    ref = raised.value.reference
    assert ref["http_status"] == 503
    assert ref["body_sha256"] == D.digest(prefix)
    assert ref["captured_body_bytes"] == len(prefix)
    assert ref["accounting_complete"] is True
    assert ref["private_retained"] is True
    observation, retained = original_observation(quarantine, reference=ref)
    assert observation["http_status"] == 503
    assert retained == prefix
    with D.Store(ledger) as restarted, pytest.raises(D.Pending, match="Unresolved"):
        source_post(T.Journal(restarted, T.HTTP(opener=opener)))
    assert len(opener.requests) == 1
    assert token.encode() not in b"".join(p.read_bytes() for p in ledger.rglob("*") if p.is_file())


@pytest.mark.parametrize("remaining", [132, 131079 + 132])
def test_insufficient_whole_response_reservation_refuses_before_effect(
    tmp_path, monkeypatch, remaining
):
    monkeypatch.setenv("GH_TOKEN", "synthetic-budget-token")
    quarantine = tmp_path / "quarantine"
    quarantine.mkdir(mode=0o700)
    opener = Opener(maximum_headers(Response(503, b"x" * 65536)))
    http = BoundaryHTTP(remaining=remaining, opener=opener, private_root=quarantine)
    with D.Store(private_root(tmp_path)) as store, pytest.raises(D.BudgetPending):
        source_post(T.Journal(store, http))
    assert not opener.requests
    assert http.budget.used <= http.budget.limit


def test_unexpected_settlement_failure_keeps_safe_observed_unavailable_pending(
    tmp_path, monkeypatch
):
    token = "ghs_SyntheticAccountingFault"
    monkeypatch.setenv("GH_TOKEN", token)
    quarantine = tmp_path / "quarantine"
    quarantine.mkdir(mode=0o700)
    opener = Opener(maximum_headers(Response(503, token.encode())))
    http = BoundaryHTTP(remaining=655368, opener=opener, private_root=quarantine)

    def broken_settlement(_reservation, *_args):
        raise RuntimeError(token)

    monkeypatch.setattr(D.ResponseReservation, "settle", broken_settlement)
    ledger = private_root(tmp_path)
    with D.Store(ledger) as store, pytest.raises(T.PrivateObservationPending) as raised:
        source_post(T.Journal(store, http))
    ref = raised.value.reference
    assert ref["http_status"] == 503
    assert ref["body_sha256"] == D.digest(token.encode())
    assert ref["captured_body_bytes"] == len(token)
    assert ref["accounting_complete"] is False
    assert ref["private_retained"] is False
    assert ref["delivery"] == "pending-unavailable"
    pending = ledger / "capture-transport" / NONCE / ("d" * 32) / "delivery-pending.json"
    assert D.parse(pending.read_bytes()) == ref
    assert token not in str(raised.value)
    assert not list(quarantine.iterdir())
    with D.Store(ledger) as restarted, pytest.raises(D.Pending, match="Unresolved"):
        source_post(T.Journal(restarted, T.HTTP(opener=opener)))
    assert len(opener.requests) == 1
    assert http.budget.used <= http.budget.limit


@pytest.mark.parametrize("remaining", [655368, 655500])
def test_near_limit_complete_known_rejection_retains_actual_refusal(
    tmp_path, monkeypatch, remaining
):
    monkeypatch.setenv("GH_TOKEN", "synthetic-budget-token")
    quarantine = tmp_path / "quarantine"
    quarantine.mkdir(mode=0o700)
    body = D.canonical({"message": "Bad credentials"})
    opener = Opener(maximum_headers(Response(401, body)))
    http = BoundaryHTTP(remaining=remaining, opener=opener, private_root=quarantine)
    with D.Store(private_root(tmp_path)) as store:
        receipt = source_post(T.Journal(store, http))
        assert receipt["state"] == "rejected"
        assert receipt["http_status"] == 401
        assert receipt["response_facts"]["body_sha256"] == D.digest(body)
        assert source_post(T.Journal(store, T.HTTP(opener=opener))) == receipt
    assert len(opener.requests) == 1
