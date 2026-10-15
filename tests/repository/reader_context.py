from __future__ import annotations

import base64
import importlib.util
import zlib

import pytest

from tests._support.paths import REPO_ROOT


def protocol():
    spec = importlib.util.spec_from_file_location(
        "trusted_task_reader", REPO_ROOT / ".github/agent/task_context.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def context(proto):
    text = "Preserve the editor's requested selection and clipboard behavior."
    question = "Which scope?\nPreserve requested behavior (Recommended): fix regressions."
    return {
        "version": 2,
        "task_id": "fixture-task",
        "producer_id": "fixture-machine",
        "source": "user",
        "starting_prompt": "Improve the editor from [Image #1].",
        "corrections": [
            "Implement the plan.",
            "Use the written geometry spec instead of the image.",
            "Preserve requested behavior",
        ],
        "plans": [{"id": proto.plan_id(text), "text": text, "approval_index": 0}],
        "images": [],
        "replacements": [{"reference": "[Image #1]", "user_index": 1}],
        "decisions": [
            {"questions": ["Which implementation?"], "choice": "Recommended implementation"}
        ],
        "questions": [{"id": proto.question_id(question, 0), "text": question, "answer_index": 2}],
    }


def test_reader_keeps_legacy_signed_task_bytes_and_authority():
    proto = protocol()
    value = {
        "version": 1,
        "task_id": "fixture-task",
        "producer_id": "fixture-machine",
        "source": "user",
        "starting_prompt": "Fix the editor.",
        "corrections": [],
    }
    assert proto.canonical(value) == (
        b'{"corrections":[],"producer_id":"fixture-machine","source":"user",'
        b'"starting_prompt":"Fix the editor.","task_id":"fixture-task","version":1}'
    )
    message = "Feature" + proto.trailers(value)
    assert "Agent-Task-Evidence:" not in message
    assert proto.parse(proto.render(value)) == value
    assert proto.verify_commit(value, message) == "fixture-machine"


def test_reader_preserves_visible_question_answer_and_distinct_automated_choices():
    proto = protocol()
    value = context(proto)
    proto.require_resolved(value)
    body = proto.render(value)
    assert "**Agent question:**" in body
    assert "Preserve requested behavior (Recommended): fix regressions." in body
    assert "**User answer:**\n<pre>Preserve requested behavior</pre>" in body
    assert "Recommended implementation" in body
    assert proto.parse(body) == value
    assert proto.verify_commit(value, "Feature" + proto.trailers(value)) == "fixture-machine"


def test_evidence_cannot_be_stripped_or_changed_under_the_old_signature():
    proto = protocol()
    value = context(proto)
    body = proto.render(value)
    signed = "Feature" + proto.trailers(value)
    stripped = body.split(proto.EVIDENCE_MARKER, 1)[0]
    legacy = proto.parse(stripped)
    with pytest.raises(ValueError, match="signed head"):
        proto.verify_commit(legacy, signed)
    value["questions"][0]["answer_index"] = 1
    with pytest.raises(ValueError, match="signed head"):
        proto.verify_commit(value, signed)


def test_reader_requires_actual_plan_and_image_records_before_author_publication():
    proto = protocol()
    value = context(proto)
    value["replacements"] = []
    with pytest.raises(ValueError, match="Missing image"):
        proto.require_resolved(value)
    value["replacements"] = [{"reference": "[Image #1]", "user_index": 1}]
    value["plans"] = []
    with pytest.raises(ValueError, match="exact approved plan"):
        proto.require_resolved(value)
    # Historical parsing remains possible; publication is the separate boundary.
    assert proto.parse(proto.render(value)) == value


@pytest.mark.parametrize("version", [1, 2])
def test_recovered_plan_reference_example_preserves_history_without_a_false_hold(version):
    proto = protocol()
    value = context(proto)
    value["starting_prompt"] = (
        "check RPI agents, their, especially those who waiting for user approval"
    )
    example = (
        "Before handoff, flag unresolved references such as "
        "\u201cImplement the plan\u201d or `[Image #1]`. "
        "Require attached evidence or an explicit user replacement of the missing requirement."
    )
    restored = "# Preserve approved plans and image evidence for agent review\n\n- " + example
    value["corrections"].append("Exact recovered approved plan follows:\n\n" + restored)
    value["replacements"] = []
    value["plans"] = [{"id": proto.plan_id(restored), "text": restored, "approval_index": 0}]
    if version == 1:
        # A quoted example alone must not manufacture a requirement in legacy history.
        value = proto.legacy(value)
        value["corrections"] = [value["corrections"][-1]]
    original = proto.canonical(value)
    signed = "Feature" + proto.trailers(value)
    proto.require_resolved(value)
    assert proto.canonical(value) == original
    assert proto.parse(proto.render(value)) == value
    assert proto.verify_commit(value, signed) == "fixture-machine"
    if version == 2:
        # The actual earlier approval request still requires its real binding.
        value["plans"] = []
        with pytest.raises(ValueError, match="exact approved plan"):
            proto.require_resolved(value)


@pytest.mark.parametrize(
    "reference",
    [
        "Use [Image #1] to implement the feature.",
        "Use `[Image #1]` to implement the feature.",
        'Use "[Image #1]" to implement the feature.',
        "Before handoff, flag unresolved references such as [Image #1].",
    ],
)
def test_reference_example_does_not_hide_genuine_missing_images(reference):
    proto = protocol()
    value = context(proto)
    example = (
        "Before handoff, flag unresolved references such as "
        "\u201cImplement the plan\u201d or `[Image #1]`. "
        "Require attached evidence or an explicit user replacement of the missing requirement."
    )
    value["starting_prompt"] = "Maintain agents."
    value["corrections"].extend([example, reference])
    value["replacements"] = []
    signed = "Feature" + proto.trailers(value)
    with pytest.raises(ValueError, match="Missing image"):
        proto.require_resolved(value)
    # Genuine replacement authority must still point to an actual correction.
    value["replacements"] = [{"reference": "[Image #1]", "user_index": 999}]
    with pytest.raises(ValueError, match="exact chronological user correction"):
        proto.require_resolved(value)
    value["replacements"] = [{"reference": "[Image #1]", "user_index": 1}]
    proto.require_resolved(value)
    with pytest.raises(ValueError, match="signed head"):
        proto.verify_commit(value, signed)


def test_reader_rejects_question_answers_outside_authenticated_chronology():
    proto = protocol()
    value = context(proto)
    value["questions"][0]["answer_index"] = len(value["corrections"])
    with pytest.raises(ValueError, match="exact chronological user correction"):
        proto.validate(value)


def test_long_actual_style_history_keeps_visible_text_and_exact_signed_payload():
    proto = protocol()
    value = context(proto)
    # Histories retain repeated option text plus separate original/current plans.
    value["corrections"].extend(["Questions: Which scope? Preserve every feature. " * 70] * 3)
    text = "Maintain complete editor semantics and preserve independent changes. " * 100
    value["plans"].append({"id": proto.plan_id(text), "text": text, "approval_index": None})
    body = proto.render(value)
    assert proto.COMPRESSED_EVIDENCE_MARKER in body
    assert len(body) <= proto.MAX_BODY
    assert text in body
    assert value["corrections"][-1] in body
    assert "**Agent question:**" in body
    assert "**User answer:**" in body
    assert proto.parse(body) == value
    assert proto.verify_commit(proto.parse(body), "Feature" + proto.trailers(value))
    with pytest.raises(ValueError, match="signed head"):
        proto.verify_commit(
            proto.parse(body.split(proto.COMPRESSED_EVIDENCE_MARKER)[0]),
            "Feature" + proto.trailers(value),
        )


@pytest.mark.parametrize(
    "payload",
    [
        b"invalid",
        zlib.compress(b"{}")[:-1],
        zlib.compress(b"x" * (512 * 1024 + 1)),
        zlib.compress(b"{}") + b"trailing",
    ],
)
def test_compressed_evidence_rejects_malformed_oversized_and_trailing_data(payload):
    proto = protocol()
    legacy = proto.legacy(context(proto))
    body = (
        proto.render(legacy)
        + proto.COMPRESSED_EVIDENCE_MARKER
        + base64.b64encode(payload).decode()
        + "\n-->\n"
    )
    with pytest.raises(ValueError, match="Invalid task attachment"):
        proto.parse(body)


def test_compressed_and_plain_supplements_cannot_compete_for_one_signature():
    proto = protocol()
    value = context(proto)
    body = (
        proto.render(value)
        + proto.COMPRESSED_EVIDENCE_MARKER
        + base64.b64encode(zlib.compress(proto.canonical(value))).decode()
        + "\n-->\n"
    )
    with pytest.raises(ValueError, match="Invalid task attachment"):
        proto.parse(body)
