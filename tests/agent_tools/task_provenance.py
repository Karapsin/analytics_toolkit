"""Fast, source-only regressions for recovered task provenance and long replies."""

from __future__ import annotations

import copy
import json

import pytest

from agent_tools import task_context
from tests._support.paths import REPO_ROOT

EVIDENCE = REPO_ROOT / "agent_tools/task_evidence/33ad72f4abee49ca97e6d058e9f81a52"
NATIVE_PLAN_ID = "88852a9c0686faeb3176b615ed69e8e626963dbaa1f358eed5ac06b2001c1dae"
HISTORICAL_DIGEST = "cb4704f218b79b51743bca07f91a8b8277c02a0b9a1bd2dfb2f8b0166d16ec78"


def test_native_recovery_preserves_history_and_exact_approval_binding() -> None:
    proto = task_context.protocol()
    history = json.loads((EVIDENCE / "history/context-before-native-recovery.json").read_text())
    current = json.loads((EVIDENCE / "context.json").read_text())
    assert proto.digest(history) == HISTORICAL_DIGEST
    assert proto.parse(proto.render(history)) == history
    assert current["starting_prompt"] == history["starting_prompt"]
    assert current["corrections"][:11] == history["corrections"]
    native, provenance = current["plans"]
    assert native["id"] == NATIVE_PLAN_ID
    assert native["text"] == history["plans"][0]["text"] + "\n"
    assert native["approval_index"] == 0
    assert native["approval_indices"] == [11]
    assert current["corrections"][0] == "Implement the plan."
    assert provenance["approval_index"] is None
    for identity in [
        HISTORICAL_DIGEST,
        "6b3e4b130a2ccdf769fa7ce0c89c51d272212d3374b1b86d7cff71f19d561e1e",
        "01a134f4-b81d-7c31-a7d4-8643fc296e65",
        "01a134fd-878f-7e50-ac92-3a0fdbeea0d1-plan",
        "01a13500-f8ef-77b3-bb75-fe5baf88e263",
        "01a13500-f903-7fb1-b59f-beef2e5983dc",
    ]:
        assert identity in provenance["text"]
    assert current["images"] == []
    assert current["replacements"] == [{"reference": "[Image #1]", "user_index": 11}]
    assert current["decisions"] == []
    proto.require_resolved(current)
    body = proto.render(current)
    assert len(body) <= proto.MAX_BODY
    assert proto.parse(body) == current
    proto.verify_commit(current, "Recovered context" + proto.trailers(current))
    assert [q["answer_index"] for q in current["questions"]] == [11, 12, 13]


def test_historical_plan_cannot_independently_resolve_current_approval() -> None:
    proto = task_context.protocol()
    current = json.loads((EVIDENCE / "context.json").read_text())
    changed = copy.deepcopy(current)
    changed["plans"] = [changed["plans"][1]]
    with pytest.raises(ValueError, match="exact approved plan"):
        proto.require_resolved(changed)
    changed = copy.deepcopy(current)
    changed["plans"][0]["text"] = changed["plans"][0]["text"].rstrip()
    with pytest.raises(ValueError, match="plan evidence"):
        proto.validate(changed)
    changed = copy.deepcopy(current)
    changed["plans"][1]["text"] += "\nChanged approval message identity."
    changed["plans"][1]["id"] = proto.plan_id(changed["plans"][1]["text"])
    with pytest.raises(ValueError, match="signed head"):
        proto.verify_commit(changed, "Recovered context" + proto.trailers(current))


@pytest.mark.parametrize("answer_size", [1024, 1025])
def test_long_answers_link_to_visible_chronological_message(answer_size: int) -> None:
    proto = task_context.protocol()
    question = "Scope?\nExisting API (Recommended): Preserve callers.\nRewrite: Replace callers."
    answer = "Use existing API. " + "x" * (answer_size - len("Use existing API. "))
    context = {
        "version": 2,
        "task_id": "fixture",
        "producer_id": "local:fixture",
        "source": "user",
        "starting_prompt": "Preserve compatibility",
        "corrections": [answer],
        "plans": [],
        "images": [],
        "replacements": [],
        "decisions": [],
        "questions": [
            {"id": proto.question_id(question, 0), "text": question, "answer_index": 0},
        ],
    }
    body = proto.render(context)
    assert "**Agent question:**\n<pre>" + question in body
    assert '<a id="task-message-1"></a>\n<pre>' + answer + "</pre>" in body
    if answer_size > 1024:
        assert "**User answer:**\n[User correction 1](#task-message-1)" in body
        assert body.count(answer) == 1
    else:
        assert "**User answer:**\n<pre>" + answer + "</pre>" in body
    assert proto.parse(body) == context
    proto.verify_commit(context, "Feature" + proto.trailers(context))


def test_reconciled_changelog_retains_feature_and_merged_dev_history() -> None:
    changelog = (REPO_ROOT / "docs/CHANGELOG.md").read_text(encoding="utf-8")
    for bullet in (
        "- Preserve plans, images and clarification context; recover Pi tooling, "
        "default recommended choices, filter alerts and support ten workers.",
        "- Fix the SQL integration startup-diagnostics guard to allow separate policy "
        "jobs while preserving required suite and diagnostic checks, with "
        "service-free regression coverage.",
        "- Recover inactive Pi reviews and corrections, supply bounded integration "
        "diagnostics, suppress unchanged no-patch repair retries, and isolate transient "
        "evidence inventory failures.",
        "- Keep Pi recovery discovery regressions within strict lint gates while "
        "preserving transient-failure and retry coverage.",
    ):
        assert changelog.count(bullet) == 1
    assert not any(
        line.startswith(("<<<<<<<", "=======", ">>>>>>>")) for line in changelog.splitlines()
    )
