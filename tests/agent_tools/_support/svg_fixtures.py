"""Decode captured SVG fixtures without normalizing their original bytes."""

from __future__ import annotations

import base64
import hashlib
import json


def decode_captured_svg(data: bytes, *, sha256: str, envelope_sha256: str) -> str:
    """Validate the storage envelope separately from the captured export identity."""
    if hashlib.sha256(data).hexdigest() != envelope_sha256:
        message = "captured_svg_envelope_hash_mismatch"
        raise ValueError(message)
    envelope = json.loads(data)
    if (
        not isinstance(envelope, dict)
        or envelope.get("schema_version") != 1
        or envelope.get("encoding") != "base64"
    ):
        message = "unsupported_captured_svg_envelope"
        raise ValueError(message)
    chunks = envelope.get("content")
    if not isinstance(chunks, list) or not chunks or any(
        not isinstance(chunk, str) or not chunk for chunk in chunks
    ):
        message = "invalid_captured_svg_chunks"
        raise ValueError(message)
    try:
        decoded = base64.b64decode("".join(chunks), validate=True)
    except ValueError as exc:
        message = "invalid_captured_svg_base64"
        raise ValueError(message) from exc
    if type(envelope.get("byte_length")) is not int or len(decoded) != envelope["byte_length"]:
        message = "captured_svg_length_mismatch"
        raise ValueError(message)
    if envelope.get("sha256") != sha256 or hashlib.sha256(decoded).hexdigest() != sha256:
        message = "captured_svg_hash_mismatch"
        raise ValueError(message)
    return decoded.decode("utf-8")
