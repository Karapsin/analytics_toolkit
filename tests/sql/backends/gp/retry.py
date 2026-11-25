from __future__ import annotations

from types import SimpleNamespace

import psycopg2
import pytest
from analytics_toolkit.sql.backends.gp.retry import classify_error
from psycopg2 import errors


class _SqlstateError(RuntimeError):
    pgcode: object = None
    sqlstate: object = None
    diag: object = None


@pytest.mark.parametrize(
    "sqlstate",
    [
        "0A000",
        "21000",
        "22003",
        "22007",
        "22012",
        "22P02",
        "23502",
        "23503",
        "23505",
        "23514",
        "28000",
        "28P01",
        "3D000",
        "3F000",
        "42501",
        "42601",
        "42804",
        "42P01",
        "44000",
        "54001",
        "57014",
        "F0000",
        "F0001",
        "XX001",
        "XX002",
    ],
)
@pytest.mark.parametrize("attribute", ["pgcode", "sqlstate", "diag"])
def test_structured_terminal_errors_do_not_require_message_matches(
    sqlstate: str, attribute: str
) -> None:
    error = _SqlstateError("neutral server message")
    value: object = SimpleNamespace(sqlstate=sqlstate) if attribute == "diag" else sqlstate
    setattr(error, attribute, value)

    assert classify_error(error) == (False, "gp_sqlstate_terminal", sqlstate)


@pytest.mark.parametrize("sqlstate", ["53100", "53200", "53400"])
def test_fixed_resource_failures_stop_by_default(sqlstate: str) -> None:
    error = _SqlstateError("neutral server message")
    error.pgcode = sqlstate

    assert classify_error(error) == (False, "gp_resource_exhausted", sqlstate)


@pytest.mark.parametrize("sqlstate", ["08007", "40003"])
def test_explicit_unknown_outcomes_are_not_replayed(sqlstate: str) -> None:
    error = _SqlstateError("neutral server message")
    error.pgcode = sqlstate

    assert classify_error(error) == (False, "gp_outcome_unknown", sqlstate)


@pytest.mark.parametrize(
    "sqlstate",
    ["40001", "40P01", "55P03", "53300", "57P01", "57P02", "57P03", "08006", "XX000", "ZZ999"],
)
def test_other_sqlstates_preserve_retry_even_with_misleading_message(sqlstate: str) -> None:
    error = _SqlstateError("syntax error inside remote payload")
    error.pgcode = sqlstate

    assert classify_error(error) == (True, "gp_sqlstate_retryable", sqlstate)


@pytest.mark.parametrize(
    ("error_class", "sqlstate"),
    [
        (errors.FeatureNotSupported, "0A000"),
        (errors.CardinalityViolation, "21000"),
        (errors.NumericValueOutOfRange, "22003"),
        (errors.InvalidDatetimeFormat, "22007"),
        (errors.DivisionByZero, "22012"),
        (errors.InvalidTextRepresentation, "22P02"),
        (errors.NotNullViolation, "23502"),
        (errors.UniqueViolation, "23505"),
        (errors.CheckViolation, "23514"),
        (errors.InvalidPassword, "28P01"),
        (errors.InvalidCatalogName, "3D000"),
        (errors.InvalidSchemaName, "3F000"),
        (errors.SyntaxError, "42601"),
        (errors.InsufficientPrivilege, "42501"),
        (errors.DatatypeMismatch, "42804"),
        (errors.UndefinedTable, "42P01"),
        (errors.WithCheckOptionViolation, "44000"),
        (errors.ProgramLimitExceeded, "54000"),
        (errors.QueryCanceled, "57014"),
        (errors.ConfigFileError, "F0000"),
        (errors.DataCorrupted, "XX001"),
        (errors.IndexCorrupted, "XX002"),
    ],
)
def test_native_class_only_terminal_errors(error_class: type[BaseException], sqlstate: str) -> None:
    assert classify_error(error_class("neutral message")) == (
        False,
        "gp_exception_class_terminal",
        sqlstate,
    )


@pytest.mark.parametrize(
    ("error_class", "sqlstate"),
    [(errors.SerializationFailure, "40001"), (errors.DeadlockDetected, "40P01")],
)
def test_native_class_only_transient_errors(
    error_class: type[BaseException], sqlstate: str
) -> None:
    assert classify_error(error_class("syntax error in remote payload")) == (
        True,
        "gp_exception_class_retryable",
        sqlstate,
    )


@pytest.mark.parametrize(
    ("error_class", "sqlstate", "reason"),
    [
        (errors.DiskFull, "53100", "gp_resource_exhausted"),
        (errors.OutOfMemory, "53200", "gp_resource_exhausted"),
        (errors.ConfigurationLimitExceeded, "53400", "gp_resource_exhausted"),
        (errors.TransactionResolutionUnknown, "08007", "gp_outcome_unknown"),
        (errors.StatementCompletionUnknown, "40003", "gp_outcome_unknown"),
    ],
)
def test_native_class_only_resource_and_unknown_outcomes(
    error_class: type[BaseException], sqlstate: str, reason: str
) -> None:
    assert classify_error(error_class("neutral message")) == (False, reason, sqlstate)


@pytest.mark.parametrize(
    "error_class",
    [
        psycopg2.DataError,
        psycopg2.IntegrityError,
        psycopg2.ProgrammingError,
        psycopg2.NotSupportedError,
    ],
)
def test_native_deterministic_base_errors_without_codes(error_class: type[BaseException]) -> None:
    assert classify_error(error_class("neutral message")) == (
        False,
        "gp_exception_class_terminal",
        None,
    )


def test_structured_sqlstate_overrides_class_name_and_normalizes_case() -> None:
    class UndefinedTableError(errors.UndefinedTable):
        pgcode = " 40p01 "

    assert classify_error(UndefinedTableError("neutral message")) == (
        True,
        "gp_sqlstate_retryable",
        "40P01",
    )


def test_invalid_pgcode_does_not_hide_diagnostic_sqlstate() -> None:
    error = _SqlstateError("neutral message")
    error.pgcode = "invalid"
    error.sqlstate = None
    error.diag = SimpleNamespace(sqlstate="22003")

    assert classify_error(error) == (False, "gp_sqlstate_terminal", "22003")


@pytest.mark.parametrize(
    "error", [RuntimeError("neutral message"), ValueError("neutral message"), TimeoutError()]
)
def test_unrecognized_errors_leave_existing_fallback_available(error: BaseException) -> None:
    assert classify_error(error) is None


def test_non_psycopg_base_class_name_does_not_claim_unknown_errors() -> None:
    class DataError(RuntimeError):
        pass

    assert classify_error(DataError("neutral message")) is None


@pytest.mark.parametrize(
    ("message", "reason"),
    [
        ('FATAL:  password authentication failed for user "reader"', "gp_startup_authentication"),
        ('FATAL: role "reader" does not exist', "gp_startup_authentication"),
        (
            'FATAL: no pg_hba.conf entry for host "192.0.2.1", user "reader", database "test"',
            "gp_startup_authentication",
        ),
        ("fe_sendauth: no password supplied", "gp_startup_authentication"),
        ('FATAL: database "missing" does not exist', "gp_startup_configuration"),
        ("SSL error: certificate verify failed", "gp_tls_certificate"),
        ("SSL error: hostname mismatch", "gp_tls_certificate"),
        ("SSL error: certificate has expired", "gp_tls_certificate"),
        ("SSL error: certificate expired", "gp_tls_certificate"),
        (
            'server certificate for "server.example" does not match host name "db.example"',
            "gp_tls_certificate",
        ),
    ],
)
@pytest.mark.parametrize("prefixed", [False, True])
def test_native_libpq_startup_failures_without_sqlstate_stop(
    message: str, reason: str, prefixed: bool
) -> None:
    if prefixed:
        message = 'connection to server at "db.example" (192.0.2.1), port 5432 failed: ' + message
    error = psycopg2.OperationalError(message)

    assert error.pgcode is None
    assert classify_error(error) == (False, reason, None)


@pytest.mark.parametrize(
    "message",
    [
        "neutral message",
        "connection refused",
        'connection to server at "db.example", port 5432 failed: Connection refused',
        'connection to server at "db.example", port 5432 failed: timeout expired',
        "connection timed out",
        "SSL error: unexpected eof while reading",
        "SSL SYSCALL error: EOF detected",
        "SSL error: tlsv1 alert internal error",
        "SELECT 'FATAL: password authentication failed for user'",
        "query failed: SSL error: certificate verify failed",
        "FATAL: terminating connection due to administrator command",
    ],
)
def test_other_native_operational_errors_keep_retry_fallback(message: str) -> None:
    assert classify_error(psycopg2.OperationalError(message)) is None


@pytest.mark.parametrize(
    "message",
    [
        'FATAL: password authentication failed for user "reader"',
        "SSL error: certificate verify failed",
    ],
)
def test_startup_message_matching_requires_native_psycopg_error(message: str) -> None:
    assert classify_error(RuntimeError(message)) is None


def test_native_operational_subclass_retains_startup_error_classification() -> None:
    class ConnectionFailure(psycopg2.OperationalError):
        pass

    error = ConnectionFailure('FATAL: password authentication failed for user "reader"')

    assert classify_error(error) == (False, "gp_startup_authentication", None)


@pytest.mark.parametrize(
    "diagnostic",
    [
        "Name or service not known",
        "Name does not resolve",
        "nodename nor servname provided, or not known",
        "No such host is known.",
    ],
)
@pytest.mark.parametrize("newline", ["", "\n", "\r\n"])
def test_native_libpq_permanent_dns_without_sqlstate_stops(diagnostic: str, newline: str) -> None:
    error = psycopg2.OperationalError(
        f'could not translate host name "bad.example" to address: {diagnostic}{newline}'
    )

    assert error.pgcode is None
    assert error.__cause__ is None
    assert classify_error(error) == (False, "gp_permanent_dns", None)


@pytest.mark.parametrize(
    "message",
    [
        (
            'could not translate host name "bad.example" to address: '
            "Temporary failure in name resolution"
        ),
        'could not translate host name "bad.example" to address: Try again',
        (
            'could not translate host name "bad.example" to address: '
            "Non-recoverable failure in name resolution"
        ),
        'could not translate host name "bad.example" to address: Non-recoverable error',
        'could not translate host name "bad.example" to address: Unknown resolver error',
        'could not translate host name "bad.example" to address: getaddrinfo failed',
        (
            'SELECT \'could not translate host name "bad.example" to address: '
            "Name or service not known'"
        ),
        (
            'SELECT\ncould not translate host name "bad.example" to address: '
            "Name or service not known"
        ),
        (
            'could not translate host name "bad.example" to address: '
            "Name or service not known; SELECT 1"
        ),
    ],
)
def test_temporary_unknown_or_quoted_dns_diagnostics_keep_fallback(message: str) -> None:
    assert classify_error(psycopg2.OperationalError(message)) is None


def test_permanent_dns_text_requires_native_psycopg_identity() -> None:
    error = RuntimeError(
        'could not translate host name "bad.example" to address: Name or service not known'
    )

    assert classify_error(error) is None


def test_structured_transient_sqlstate_overrides_permanent_dns_text() -> None:
    class DnsOperationalError(psycopg2.OperationalError):
        sqlstate = "08006"

    error = DnsOperationalError(
        'could not translate host name "bad.example" to address: Name or service not known'
    )

    assert classify_error(error) == (True, "gp_sqlstate_retryable", "08006")


def test_native_transient_class_overrides_permanent_dns_text() -> None:
    error = errors.SerializationFailure(
        'could not translate host name "bad.example" to address: Name or service not known'
    )

    assert classify_error(error) == (True, "gp_exception_class_retryable", "40001")
