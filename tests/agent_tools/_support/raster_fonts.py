"""Observe a glyph-capable synthetic-test font without claiming renderer selection."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any

SYNTHETIC_FAMILY = "Noto Sans CJK JP"
_MAX_PROBE_BYTES = 64 * 1024
_MAX_FONT_BYTES = 32 * 1024 * 1024


def _covered(charset: str, character: str) -> bool:
    codepoint = ord(character)
    for interval in charset.split():
        first, separator, last = interval.partition("-")
        if int(first, 16) <= codepoint <= int(last if separator else first, 16):
            return True
    return False


def observe_test_font(text: str) -> dict[str, Any]:
    arguments = [
        "fc-match",
        "--format=%{family}\n%{file}\n%{index}\n%{charset}",
        SYNTHETIC_FAMILY,
    ]
    record: dict[str, Any] = {
        "arguments": arguments,
        "complete": False,
        "renderer_selection": "unobservable; fontconfig coverage is not renderer selection",
    }
    try:
        result = subprocess.run(arguments, capture_output=True, check=False, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        record.update(missing=type(exc).__name__, message=str(exc))
        return record
    record["exit_code"] = result.returncode
    if max(len(result.stdout), len(result.stderr)) > _MAX_PROBE_BYTES:
        record["missing"] = "font_probe_byte_limit"
        return record
    record.update(stdout=result.stdout.decode("utf-8"), stderr=result.stderr.decode("utf-8"))
    if result.returncode:
        record["missing"] = "font_probe_failed"
        return record
    family, filename, index, charset = record["stdout"].split("\n", 3)
    path = Path(filename)
    try:
        with path.open("rb") as stream:
            data = stream.read(_MAX_FONT_BYTES + 1)
        if len(data) > _MAX_FONT_BYTES:
            record["missing"] = "font_file_byte_limit"
            return record
        coverage = {f"U+{ord(character):04X}": _covered(charset, character) for character in text}
    except (OSError, ValueError) as exc:
        record.update(missing=type(exc).__name__, message=str(exc))
        return record
    record.update(
        family=family,
        file={
            "path": filename,
            "face_index": index,
            "byte_length": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        },
        character_coverage=coverage,
        missing_glyph_control_covered=_covered(charset, "\U0010ffff"),
        complete=SYNTHETIC_FAMILY in family.split(",") and all(coverage.values()),
    )
    return record
