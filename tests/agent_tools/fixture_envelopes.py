from __future__ import annotations

import base64
import hashlib
import json

import pytest

from tests._support.paths import REPO_ROOT
from tests.agent_tools._support.svg_fixtures import decode_captured_svg

_CAPTURED = b"<svg>  \n<text>e\xcc\x81 \t</text> \n</svg>"


def _envelope() -> dict[str, object]:
    encoded = base64.b64encode(_CAPTURED).decode("ascii")
    return {
        "schema_version": 1,
        "encoding": "base64",
        "byte_length": len(_CAPTURED),
        "sha256": hashlib.sha256(_CAPTURED).hexdigest(),
        "content": [encoded[:12], encoded[12:]],
    }


def _decode(envelope: dict[str, object]) -> str:
    data = (json.dumps(envelope, indent=2) + "\n").encode("utf-8")
    return decode_captured_svg(
        data,
        sha256=hashlib.sha256(_CAPTURED).hexdigest(),
        envelope_sha256=hashlib.sha256(data).hexdigest(),
    )


def test_envelope_replay_preserves_unicode_tabs_and_trailing_whitespace() -> None:
    assert _decode(_envelope()).encode("utf-8") == _CAPTURED


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("schema_version", 2, "unsupported"),
        ("encoding", "UTF-8", "unsupported"),
        ("content", [], "chunks"),
        ("content", ["!!!"], "base64"),
        ("content", ["YQ"], "base64"),
        ("content", ["é"], "base64"),
        ("content", [123], "chunks"),
        ("byte_length", len(_CAPTURED) + 1, "length_mismatch"),
        ("byte_length", True, "length_mismatch"),
        ("sha256", "0" * 64, "hash_mismatch"),
    ],
)
def test_invalid_storage_is_rejected_before_svg_replay(
    field: str, value: object, reason: str
) -> None:
    envelope = _envelope()
    envelope[field] = value
    with pytest.raises(ValueError, match=reason):
        _decode(envelope)


def test_changed_decoded_bytes_cannot_retain_the_captured_identity() -> None:
    envelope = _envelope()
    changed = _CAPTURED.replace(b"svg", b"SVG")
    assert len(changed) == len(_CAPTURED)
    envelope["content"] = [base64.b64encode(changed).decode("ascii")]
    with pytest.raises(ValueError, match="captured_svg_hash_mismatch"):
        _decode(envelope)


def test_envelope_identity_is_checked_separately_from_the_decoded_hash() -> None:
    data = json.dumps(_envelope()).encode("utf-8")
    with pytest.raises(ValueError, match="envelope_hash_mismatch"):
        decode_captured_svg(
            data + b" ",
            sha256=hashlib.sha256(_CAPTURED).hexdigest(),
            envelope_sha256=hashlib.sha256(data).hexdigest(),
        )


@pytest.mark.parametrize("scene", ["editor-command-caret", "keyboard-multicursor"])
def test_retained_exports_decode_to_the_original_provenance_identity(scene: str) -> None:
    directory = REPO_ROOT / "tests/agent_tools/_support/fixtures"
    provenance = json.loads((directory / "caret-export-provenance.json").read_bytes())
    fixture = next(item for item in provenance["fixtures"] if item["scene"] == scene)
    data = (directory / fixture["path"]).read_bytes()
    svg = decode_captured_svg(
        data, sha256=fixture["sha256"], envelope_sha256=fixture["envelope_sha256"]
    )
    decoded = svg.encode("utf-8")
    assert len(decoded) == fixture["byte_length"]
    assert hashlib.sha256(decoded).hexdigest() == fixture["sha256"]
    assert not any(line.rstrip() != line for line in data.decode("utf-8").splitlines())
