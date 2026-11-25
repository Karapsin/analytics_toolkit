from __future__ import annotations

import re

from .ch.retry import _LEGACY_TERMINAL_NAMES, TERMINAL_ERROR_CODES
from .trino.retry import NON_RETRYABLE_ERROR_REASONS

_MAX_NATIVE_ERROR_CODE = (1 << 32) - 1
_MAX_CODE_CHARACTERS = 80
_MAX_NUMERIC_CODE_CHARACTERS = 10
_SQLSTATE = re.compile(r"[0-9A-Z]{5}\Z")
_IDENTIFIER = re.compile(r"[A-Z][A-Z0-9_]{0,79}\Z")
_TRUSTED_IDENTIFIERS = {
    "ch": frozenset(name for name, _reason in TERMINAL_ERROR_CODES.values())
    | _LEGACY_TERMINAL_NAMES,
    "trino": frozenset(NON_RETRYABLE_ERROR_REASONS),
}


def safe_error_code(backend: str | None, code: str | int | None) -> str | None:
    """Return a bounded diagnostic code without echoing arbitrary server metadata."""
    if type(code) is int:
        return str(code) if 0 <= code <= _MAX_NATIVE_ERROR_CODE else None
    if not isinstance(code, str) or len(code) > _MAX_CODE_CHARACTERS:
        return None
    if backend == "gp" and _SQLSTATE.fullmatch(code):
        return code
    if code.isascii() and code.isdecimal() and len(code) <= _MAX_NUMERIC_CODE_CHARACTERS:
        return code if int(code) <= _MAX_NATIVE_ERROR_CODE else None
    trusted = _TRUSTED_IDENTIFIERS.get(backend or "", frozenset())
    return code if _IDENTIFIER.fullmatch(code) and code in trusted else None
