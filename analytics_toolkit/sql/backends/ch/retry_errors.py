"""Native permission errors remain deterministic across ClickHouse transports."""

from __future__ import annotations

import re

_PERMISSION_CODES = frozenset({291, 497})  # DATABASE_ACCESS_DENIED and ACCESS_DENIED.
_HTTP_EXCEPTION_CODE = re.compile(r"Received ClickHouse exception, code:\s*(\d+)\b", re.IGNORECASE)
_SERVER_EXCEPTION_CODE = re.compile(r"Code:\s*(\d+)\b", re.IGNORECASE)
_HTTP_STATUS_PREFIX = "HTTP driver received HTTP status "
_HTTP_BODY_SEPARATOR = ", server response:"


def is_permission_error(error: Exception) -> bool:
    code = getattr(error, "code", None)
    if isinstance(code, int):
        return code in _PERMISSION_CODES
    message = " ".join(str(part) for part in error.args)
    code_match = _HTTP_EXCEPTION_CODE.match(message)
    if code_match is not None:
        return int(code_match[1]) in _PERMISSION_CODES
    if message.startswith(_HTTP_STATUS_PREFIX):
        _header, separator, body = message.partition(_HTTP_BODY_SEPARATOR)
        if not separator:
            return False
        message = body.lstrip()
    if "db::exception" in message.casefold():
        code_match = _SERVER_EXCEPTION_CODE.match(message)
        return code_match is not None and int(code_match[1]) in _PERMISSION_CODES
    return False
