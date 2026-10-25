"""Whole-set label replacement preserves observed labels and private authority."""

from __future__ import annotations

import copy
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.repository.review_control import control as control  # noqa: PLC0414
from tests.repository.status_producers import ProducerGitHub, authority, insert_question, names


class PagedGitHub(ProducerGitHub):
    def __init__(self) -> None:
        super().__init__("agent:queued")
        self.reads: list[str] = []
        self.written: list[set[str]] = []
        self.fault: Any = None

    def api(self, path: str, method: str = "GET", data: Any = None) -> Any:
        if method == "GET" and urlsplit(path).path.endswith("/labels"):
            self.reads.append(path)
            query = parse_qs(urlsplit(path).query)
            assert query["per_page"] == ["100"]
            page = int(query["page"][0])
            return copy.deepcopy(self.pr["labels"][(page - 1) * 100 : page * 100])
        if method == "PUT":
            self.written.append(set(data["labels"]))
            if self.fault:
                return self.fault(path, data)
        return super().api(path, method, data)


@pytest.mark.parametrize("phase", ["agent:reviewing", "agent:writing", "agent:queued"])
def test_one_replacement_preserves_every_unrelated_label_and_is_idempotent(
    control: Any,
    phase: str,
) -> None:
    gh = PagedGitHub()
    gh.pr["labels"] += [{"name": f"feature-{i}"} for i in range(105)]
    gh.pr["labels"] += [{"name": "agent:semantic"}]
    unrelated = names(gh) - control.PHASES
    before = authority(control)
    assert control.status_transition(gh, 1, "a" * 40, lambda _: phase)
    assert names(gh) == unrelated | {phase}
    assert all(method == "PUT" for _, method in gh.mutations)
    assert len(gh.written) == (phase != "agent:queued")
    assert any("page=2" in path for path in gh.reads)
    count = len(gh.written)
    assert control.status_transition(gh, 1, "a" * 40, lambda _: phase)
    assert len(gh.written) == count
    assert authority(control) == before


@pytest.mark.parametrize("count", [100, 1000])
def test_exact_full_page_boundary_requires_terminal_page(control: Any, count: int) -> None:
    gh = PagedGitHub()
    gh.pr["labels"] = [{"name": f"feature-{i}"} for i in range(count - 1)]
    gh.pr["labels"].append({"name": "agent:queued"})
    assert control.status_transition(gh, 1, "a" * 40, lambda _: "agent:writing")
    assert len(names(gh)) == count
    assert any(f"page={count // 100 + 1}" in path for path in gh.reads)
    assert len(gh.written) == 1


@pytest.mark.parametrize("failure", ["overflow", "repeated", "malformed", "auth"])
def test_incomplete_or_invalid_pagination_never_writes(control: Any, failure: str) -> None:
    gh = PagedGitHub()
    gh.pr["labels"] = [{"name": f"feature-{i}"} for i in range(1001)]
    original = gh.api

    def api(path: str, method: str = "GET", data: Any = None) -> Any:
        if method == "GET" and "page=2" in path:
            if failure == "auth":
                message = "authenticated read failed"
                raise PermissionError(message)
            if failure == "malformed":
                return [{"name": True}]
            if failure == "repeated":
                return [{"name": "feature-0"}]
        return original(path, method, data)

    gh.api = api
    with pytest.raises((ValueError, PermissionError)):
        control.status_transition(gh, 1, "a" * 40, lambda _: "agent:writing")
    assert not gh.written
    assert not gh.mutations


@pytest.mark.parametrize("owner_question", [False, True])
def test_outgoing_overflow_refuses_phase_and_attention_before_any_write(
    control: Any,
    owner_question: bool,
) -> None:
    gh = PagedGitHub()
    gh.pr["labels"] = [{"name": f"feature-{i}"} for i in range(1000)]
    if owner_question:
        insert_question(control, gh)
    before = authority(control)
    with pytest.raises(ValueError, match=r"Projected.*capacity"):
        control.status_transition(gh, 1, "a" * 40, lambda _: "agent:writing")
    assert len(names(gh)) == 1000
    assert not gh.written
    assert not gh.mutations
    assert authority(control) == before


@pytest.mark.parametrize("accepted", [True, False])
def test_lost_response_reads_current_labels_without_blind_repeat(
    control: Any,
    accepted: bool,
) -> None:
    gh = PagedGitHub()

    def fault(path: str, data: Any) -> Any:
        if accepted:
            gh.pr["labels"] = [{"name": label} for label in data["labels"]]
        gh.reads.clear()
        message = "response lost"
        raise ConnectionError(message)

    gh.fault = fault
    if accepted:
        assert control.status_transition(gh, 1, "a" * 40, lambda _: "agent:writing")
        assert names(gh) == {"feature", "agent:writing"}
    else:
        with pytest.raises(ConnectionError, match="response lost"):
            control.status_transition(gh, 1, "a" * 40, lambda _: "agent:writing")
        assert names(gh) == {"feature", "agent:queued"}
    assert gh.reads  # Acknowledgment failure is followed by authenticated GET.
    assert len(gh.written) == 1


def test_final_projection_preserves_new_unrelated_label_and_live_activity(control: Any) -> None:
    gh = PagedGitHub()
    calls = 0

    def choose(_fresh: Any) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            gh.pr["labels"].append({"name": "agent:semantic"})
            return "agent:reviewing"
        return "agent:writing"

    assert control.status_transition(gh, 1, "a" * 40, choose)
    assert gh.written == [{"feature", "agent:semantic", "agent:writing"}]


def test_unknown_projection_preserves_phase_and_closed_cleanup_only_removes_owned(
    control: Any,
) -> None:
    gh = PagedGitHub()
    assert control.status_transition(gh, 1, "a" * 40, lambda _: None)
    assert not gh.written
    gh.pr.update(state="closed")
    gh.pr["labels"].append({"name": control.PAUSED})
    assert control.status_transition(gh, 1, "a" * 40, lambda _: "", expected_state="closed")
    assert names(gh) == {"feature", control.PAUSED}


def test_malformed_acknowledgment_can_only_converge_via_fresh_read(control: Any) -> None:
    gh = PagedGitHub()

    def fault(path: str, data: Any) -> Any:
        gh.pr["labels"] = [{"name": label} for label in data["labels"]]
        gh.reads.clear()
        return {"untrusted": "ack"}

    gh.fault = fault
    assert control.status_transition(gh, 1, "a" * 40, lambda _: "agent:reviewing")
    assert gh.reads
    assert len(gh.written) == 1
