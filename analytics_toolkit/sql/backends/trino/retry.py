from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

NON_RETRYABLE_ERROR_REASONS: dict[str, str] = {
    # Keep historical wrapper aliases terminal for both name and error_name.
    **dict.fromkeys(
        ("FEATURE_NOT_SUPPORTED", "INSUFFICIENT_PRIVILEGE"), "legacy_deterministic_error"
    ),
    **dict.fromkeys(
        (
            "SYNTAX_ERROR",
            "CATALOG_NOT_FOUND",
            "SCHEMA_NOT_FOUND",
            "TABLE_NOT_FOUND",
            "COLUMN_NOT_FOUND",
            "FUNCTION_NOT_FOUND",
            "ALREADY_EXISTS",
            "TYPE_MISMATCH",
            "INVALID_CAST_ARGUMENT",
            "INVALID_FUNCTION_ARGUMENT",
            "DIVISION_BY_ZERO",
            "NUMERIC_VALUE_OUT_OF_RANGE",
            "PERMISSION_DENIED",
            "CONSTRAINT_VIOLATION",
            "NOT_SUPPORTED",
            "INVALID_SESSION_PROPERTY",
            "INVALID_TABLE_PROPERTY",
            "CONFIGURATION_INVALID",
            "COMPILER_ERROR",
            "QUERY_EXCEEDED_COMPILER_LIMIT",
        ),
        "trino_invalid_query",
    ),
    **dict.fromkeys(("USER_CANCELED", "ADMINISTRATIVELY_KILLED"), "trino_cancelled"),
    **dict.fromkeys(
        (
            "EXCEEDED_GLOBAL_MEMORY_LIMIT",
            "EXCEEDED_LOCAL_MEMORY_LIMIT",
            "EXCEEDED_FUNCTION_MEMORY_LIMIT",
            "EXCEEDED_TIME_LIMIT",
            "EXCEEDED_CPU_LIMIT",
            "EXCEEDED_SPILL_LIMIT",
            "EXCEEDED_SCAN_LIMIT",
            "EXCEEDED_WRITE_LIMIT",
            "EXCEEDED_OUTPUT_LIMIT",
        ),
        "trino_fixed_limit",
    ),
    **dict.fromkeys(
        (
            "HIVE_BAD_DATA",
            "HIVE_PARTITION_SCHEMA_MISMATCH",
            "HIVE_INVALID_METADATA",
            "HIVE_UNSUPPORTED_FORMAT",
            "ICEBERG_BAD_DATA",
            "ICEBERG_INVALID_METADATA",
            "ICEBERG_INVALID_PARTITION_VALUE",
        ),
        "trino_invalid_connector_data",
    ),
}

_QUERY_ERROR_CLASSES = {
    "TrinoQueryError",
    "TrinoUserError",
    "TrinoInternalError",
    "TrinoExternalError",
}
_HTTP_ERROR_CLASSES = {"HttpError", "Http502Error", "Http503Error", "Http504Error"}
_HTTP_AUTHENTICATION_STATUSES = {401, 403}
_HTTP_TRANSIENT_STATUSES = {429, 502, 503, 504}
_HTTP_NOT_FOUND = 404


def classify_error(exc: BaseException) -> tuple[bool, str, str | int | None] | None:
    """Classify native Trino metadata without importing an optional driver."""
    class_names = {cls.__name__ for cls in type(exc).mro()}
    if "TrinoAuthError" in class_names:
        return False, "trino_authentication", None

    payload = _error_payload(exc)
    error_name = _error_name(
        _attribute(exc, "error_name") or _attribute(exc, "name") or payload.get("errorName")
    )
    error_type = _error_name(_attribute(exc, "error_type") or payload.get("errorType"))
    native_code = _attribute(exc, "error_code")
    if native_code is None:
        native_code = payload.get("errorCode")
    code = error_name or (native_code if isinstance(native_code, (str, int)) else None)
    if error_name is not None and error_name in NON_RETRYABLE_ERROR_REASONS:
        return False, NON_RETRYABLE_ERROR_REASONS[error_name], code
    if error_name or error_type or class_names & _QUERY_ERROR_CLASSES:
        # Unknown and transient native failures retain the existing retry policy;
        # SQL text embedded in their messages must not override native metadata.
        return True, "trino_legacy_retry", code
    if any(
        cls.__name__ in _HTTP_ERROR_CLASSES and cls.__module__.startswith("trino.")
        for cls in type(exc).mro()
    ):
        return _classify_http_error(exc)
    return None


def _attribute(value: Any, name: str) -> Any:
    try:
        return getattr(value, name, None)
    except (AttributeError, TypeError):
        return None


def _error_payload(exc: BaseException) -> Mapping[str, Any]:
    payload = _attribute(exc, "_error")
    if isinstance(payload, Mapping):
        return payload
    for argument in exc.args:
        if isinstance(argument, Mapping):
            return argument
    return {}


def _error_name(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip().upper()
    return None


def _classify_http_error(exc: BaseException) -> tuple[bool, str, str | int | None] | None:
    response = _attribute(exc, "response")
    status = _attribute(response, "status_code") or _attribute(exc, "status_code")
    if not isinstance(status, int):
        # Both minimum and current clients put only this status prefix in args.
        message = exc.args[0] if exc.args else None
        match = re.match(r"^error ([0-9]{3})(?::|$)", message) if isinstance(message, str) else None
        status = int(match.group(1)) if match is not None else None
    if status in _HTTP_AUTHENTICATION_STATUSES:
        return False, "trino_authentication", status
    if status == _HTTP_NOT_FOUND and _is_submit_endpoint(response, exc):
        return False, "trino_invalid_endpoint", status
    if status in _HTTP_TRANSIENT_STATUSES:
        return True, "trino_legacy_retry", status
    return None


def _is_submit_endpoint(response: Any, exc: BaseException) -> bool:
    request = _attribute(response, "request") or _attribute(exc, "request")
    method = _attribute(request, "method")
    url = _attribute(request, "url")
    if not isinstance(method, str) or method.upper() != "POST" or not isinstance(url, str):
        return False
    try:
        return urlsplit(url).path.rstrip("/") == "/v1/statement"
    except ValueError:
        return False
