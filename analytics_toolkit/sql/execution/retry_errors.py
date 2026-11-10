from __future__ import annotations

import socket
import ssl
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

from analytics_toolkit.sql.connection.errors import (
    InvalidSqlInputError,
    SqlConfigError,
    UnsupportedConnectionTypeError,
)

from .backend_options import preserve_recovery_artifacts


@dataclass(frozen=True)
class RetryDecision:
    retryable: bool
    reason: str
    backend: str | None = None
    code: str | int | None = None


def classify_sql_error(exc: BaseException, *, backend: str | None = None) -> RetryDecision:
    """Recognize terminal failures while retaining the legacy unmatched retry policy."""
    if preserve_recovery_artifacts(failed=True):
        return RetryDecision(retryable=False, reason="recovery_artifacts_preserved")
    nodes = tuple(_exception_chain(exc, backend=backend))
    marked_terminal = False
    for node, _origin in nodes:
        if getattr(node, "analytics_toolkit_sql_retry_safe", True) is False:
            marked_terminal = True
        reason = _common_terminal_reason(node)
        if reason is not None:
            return RetryDecision(retryable=False, reason=reason)

    recognized = _backend_decision(nodes)
    if recognized is not None and not recognized.retryable:
        return recognized
    if marked_terminal:
        return RetryDecision(
            retryable=False,
            reason="explicit_terminal_marker",
            backend=recognized.backend if recognized else None,
            code=recognized.code if recognized else None,
        )
    # Do not reinterpret SQL excerpts in a structured server error as its cause.
    if recognized is not None:
        return recognized
    terminal = any(_legacy_terminal_error(node) for node, _origin in nodes)
    return RetryDecision(
        retryable=not terminal,
        reason="legacy_deterministic_error" if terminal else "legacy_unclassified_error",
    )


def _backend_decision(nodes: tuple[tuple[BaseException, str | None], ...]) -> RetryDecision | None:
    # Adapters also import execution helpers, so defer this dispatch import.
    from analytics_toolkit.sql.backends.retry import classify_backend_error  # noqa: PLC0415

    recognized: RetryDecision | None = None
    for node, origin_hint in nodes:
        result = classify_backend_error(node, backend=origin_hint)
        if result is not None:
            origin, (retryable, reason, code) = result
            decision = RetryDecision(retryable=retryable, reason=reason, backend=origin, code=code)
            if not retryable:
                return decision
            if recognized is None:
                recognized = decision
    return recognized


def _exception_chain(
    exc: BaseException, *, backend: str | None = None
) -> Iterator[tuple[BaseException, str | None]]:
    pending = [(exc, backend)]
    seen: set[int] = set()
    while pending:
        current, inherited_origin = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        origin = (
            getattr(current, "analytics_toolkit_sql_backend", None)
            or getattr(current, "backend", None)
            or inherited_origin
        )
        yield current, origin
        cause = current.__cause__
        if cause is not None:
            pending.append((cause, origin))
        elif not current.__suppress_context__ and current.__context__ is not None:
            pending.append((current.__context__, origin))
        for field in ("original_exception", "original_error", "nested", "reason"):
            nested = getattr(current, field, None)
            if isinstance(nested, BaseException):
                pending.append((nested, origin))
        # SSL/DNS failures are sometimes retained only in HTTP wrapper arguments.
        pending.extend((part, origin) for part in current.args if isinstance(part, BaseException))


def _common_terminal_reason(exc: BaseException) -> str | None:
    reasons: tuple[tuple[tuple[type[BaseException], ...], str], ...] = (
        ((SqlConfigError, UnsupportedConnectionTypeError), "invalid_connection_configuration"),
        ((InvalidSqlInputError,), "invalid_sql_input"),
        ((ImportError,), "missing_driver_or_dependency"),
        ((ssl.SSLCertVerificationError,), "certificate_verification_failed"),
    )
    for error_types, reason in reasons:
        if isinstance(exc, error_types):
            return reason
    return _transport_terminal_reason(exc)


def _transport_terminal_reason(exc: BaseException) -> str | None:
    if isinstance(exc, socket.gaierror) and exc.errno in {
        socket.EAI_NONAME,
        getattr(socket, "EAI_NODATA", socket.EAI_NONAME),
    }:
        return "permanent_dns_failure"
    class_names = {cls.__name__ for cls in type(exc).__mro__}
    modules = tuple(cls.__module__ for cls in type(exc).__mro__)
    if class_names & {"InvalidURL", "InvalidSchema", "MissingSchema", "URLRequired"} and any(
        module.startswith("requests.") for module in modules
    ):
        return "invalid_connection_url"
    message = str(exc).lower()
    is_ssl = bool(class_names & {"SSLError", "CertificateError", "SSLCertVerificationError"})
    if is_ssl and any(
        part in message
        for part in (
            "certificate verify failed",
            "certificate_verify_failed",
            "hostname mismatch",
            "certificate has expired",
            "doesn't match",
            "does not match",
        )
    ):
        return "certificate_verification_failed"
    return None


def _legacy_terminal_error(exc: BaseException) -> bool:
    class_names = {cls.__name__ for cls in type(exc).__mro__}
    if class_names & {"SchemaNotFoundError", "TableNotFoundError"}:
        return True
    name = str(getattr(exc, "error_name", "") or getattr(exc, "name", "")).upper()
    if name in _LEGACY_TERMINAL_NAMES:
        return True
    message = " ".join(str(part) for part in exc.args if part).lower() or str(exc).lower()
    if any(pattern in message for pattern in _LEGACY_MESSAGE_PATTERNS):
        return True
    missing = "does not exist" in message or "doesn't exist" in message
    return missing and ("table" in message or "type " in message)


_LEGACY_TERMINAL_NAMES = frozenset(
    {
        "ALREADY_EXISTS",
        "BAD_ARGUMENTS",
        "COLUMN_NOT_FOUND",
        "FEATURE_NOT_SUPPORTED",
        "FUNCTION_NOT_FOUND",
        "ILLEGAL_TYPE_OF_ARGUMENT",
        "INSUFFICIENT_PRIVILEGE",
        "SCHEMA_NOT_FOUND",
        "SYNTAX_ERROR",
        "TABLE_NOT_FOUND",
        "TYPE_MISMATCH",
        "UNKNOWN_IDENTIFIER",
        "UNKNOWN_TABLE",
    }
)
_LEGACY_MESSAGE_PATTERNS = (
    "sql must not return duplicate columns:",
    "trino table operations for schema-qualified names require",
    "trino table operations for unqualified names require",
    "syntax error",
    "syntax_error",
    "mismatched input",
    "table not found",
    "table_not_found",
    "relation does not exist",
    "unknown table",
    "unknown_identifier",
    "can't adapt type",
    "no such table",
    "undefined table",
    "undefined_table",
    "is ambiguous",
    "must appear in the group by clause",
    "illegal type of argument",
    "illegal value (aggregate function) for positional argument in group by",
    "cross-database references are not implemented",
    "must be owner of relation",
    "must be owner of table",
    "permission denied",
)
