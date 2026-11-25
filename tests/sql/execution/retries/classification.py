from __future__ import annotations

import socket
import ssl
from types import SimpleNamespace

import pytest
from analytics_toolkit.sql.connection.errors import (
    InvalidSqlInputError,
    SqlConfigError,
    UnsupportedConnectionTypeError,
)
from analytics_toolkit.sql.dml.transfer.runtime.retry import (
    is_non_retryable_sql_error,
    run_with_retry,
)
from analytics_toolkit.sql.execution.retry_errors import classify_sql_error
from requests.exceptions import InvalidURL, SSLError


class NativeClickHouseError(Exception):
    __module__ = "clickhouse_driver.errors"

    def __init__(self, code: int, message: str = "neutral diagnostic") -> None:
        super().__init__(message)
        self.code = code


class NativeGpError(Exception):
    def __init__(self, sqlstate: str, message: str = "neutral diagnostic") -> None:
        super().__init__(message)
        self.pgcode = sqlstate


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (SqlConfigError("neutral"), "invalid_connection_configuration"),
        (UnsupportedConnectionTypeError("neutral"), "invalid_connection_configuration"),
        (InvalidSqlInputError("neutral"), "invalid_sql_input"),
        (ImportError("optional driver absent"), "missing_driver_or_dependency"),
        (ssl.SSLCertVerificationError(1, "neutral"), "certificate_verification_failed"),
        (InvalidURL("neutral"), "invalid_connection_url"),
        (socket.gaierror(socket.EAI_NONAME, "neutral"), "permanent_dns_failure"),
        (SSLError("certificate verify failed"), "certificate_verification_failed"),
    ],
)
def test_known_setup_failure_stops_before_sleep(monkeypatch, error, reason) -> None:
    attempts: list[int] = []
    sleeps: list[float] = []
    monkeypatch.setattr(
        "analytics_toolkit.sql.dml.transfer.runtime.retry.time.sleep", sleeps.append
    )

    def operation(attempt: int) -> None:
        attempts.append(attempt)
        raise error

    with pytest.raises(type(error)) as caught:
        run_with_retry("setup", 3, 5, operation)

    assert caught.value is error
    assert attempts == [1]
    assert sleeps == []
    assert classify_sql_error(error).reason == reason


@pytest.mark.parametrize(
    "error",
    [
        ValueError("temporary response conversion failure"),
        TypeError("unclassified application failure"),
        RuntimeError("unclassified application failure"),
        ConnectionError("lost commit acknowledgement"),
        TimeoutError("temporary timeout"),
        socket.gaierror(socket.EAI_AGAIN, "temporary lookup failure"),
        SSLError("unexpected EOF"),
    ],
)
def test_unmatched_failures_keep_legacy_retry(error) -> None:
    attempts: list[int] = []

    def operation(attempt: int) -> str:
        attempts.append(attempt)
        if attempt == 1:
            raise error
        return "complete"

    assert run_with_retry("legacy retry", 2, 0, operation) == "complete"
    assert attempts == [1, 2]


@pytest.mark.parametrize(
    "reference", ["__cause__", "original_exception", "original_error", "nested", "reason"]
)
def test_terminal_native_error_survives_wrappers_and_cycles(reference) -> None:
    native = NativeClickHouseError(516)
    wrapper = RuntimeError("stage or HTTP operation failed")
    setattr(wrapper, reference, native)
    native.__cause__ = wrapper

    decision = classify_sql_error(wrapper)

    assert decision.retryable is False
    assert decision.backend == "ch"
    assert decision.code == 516


def test_nested_terminal_marker_overrides_native_transient() -> None:
    original = ConnectionError("temporary transport failure")
    original.analytics_toolkit_sql_retry_safe = False
    wrapper = NativeClickHouseError(210)
    wrapper.__cause__ = original

    assert is_non_retryable_sql_error(wrapper)


def test_tagged_terminal_keeps_native_diagnostic_for_logging() -> None:
    error = NativeClickHouseError(516)
    error.analytics_toolkit_sql_retry_safe = False
    decision = classify_sql_error(error)
    assert decision.retryable is False
    assert decision.code == 516
    assert decision.reason == "clickhouse_authentication"


def test_unsuppressed_context_is_inspected_but_suppressed_context_is_not() -> None:
    original = NativeGpError("28P01")
    wrapper = RuntimeError("operation failed")
    wrapper.__context__ = original
    assert is_non_retryable_sql_error(wrapper)

    wrapper.__suppress_context__ = True
    assert not is_non_retryable_sql_error(wrapper)


def test_tls_failure_retained_only_as_http_argument_stops() -> None:
    original = ssl.SSLCertVerificationError(1, "neutral")
    wrapper = ConnectionError("transport error", original)
    assert classify_sql_error(wrapper).reason == "certificate_verification_failed"


@pytest.mark.parametrize("destination", ["ch", "trino"])
def test_native_source_state_wins_over_destination_context(destination) -> None:
    error = NativeGpError("22P02")
    error.sql_context = SimpleNamespace(backend=destination)

    decision = classify_sql_error(error, backend=destination)

    assert decision.retryable is False
    assert decision.backend == "gp"
    assert decision.code == "22P02"


def test_clickhouse_native_code_is_not_interpreted_as_another_engine() -> None:
    error = NativeClickHouseError(516)
    error.sql_context = SimpleNamespace(backend="trino")
    decision = classify_sql_error(error, backend="gp")
    assert decision.retryable is False
    assert decision.backend == "ch"


def test_trusted_boundary_origin_classifies_numeric_only_error() -> None:
    error = RuntimeError("neutral diagnostic")
    error.code = 516
    error.analytics_toolkit_sql_backend = "ch"
    error.sql_context = SimpleNamespace(backend="trino")
    assert is_non_retryable_sql_error(error)


def test_arbitrary_numeric_error_without_origin_remains_unclassified() -> None:
    error = RuntimeError("neutral diagnostic")
    error.code = 516
    assert classify_sql_error(error).reason == "legacy_unclassified_error"


@pytest.mark.parametrize("attribute", ["error_name", "name"])
@pytest.mark.parametrize("name", ["AUTHENTICATION_FAILED", "EXCEEDED_TIME_LIMIT"])
def test_known_symbolic_error_is_classified_without_a_driver_import(attribute, name) -> None:
    error = RuntimeError("neutral diagnostic")
    setattr(error, attribute, name)
    assert is_non_retryable_sql_error(error)


@pytest.mark.parametrize(
    "message",
    [
        "HTTP driver received HTTP status 401, server response: Authentication required",
        "HTTPDriver for https://localhost:8443 returned response code 403)",
        ":HTTPDriver for https://localhost:8443 returned response code 500)\n "
        "Code: 516. DB::Exception: Authentication failed.",
    ],
)
def test_supported_clickhouse_http_diagnostics_retain_engine_identity(message) -> None:
    decision = classify_sql_error(RuntimeError(message))
    assert decision.retryable is False
    assert decision.backend == "ch"


def test_source_wrapper_origin_is_inherited_by_numeric_only_cause() -> None:
    error = RuntimeError("neutral diagnostic")
    error.code = 516
    wrapper = RuntimeError("source stream failed")
    wrapper.analytics_toolkit_sql_backend = "ch"
    wrapper.__cause__ = error
    wrapper.sql_context = SimpleNamespace(backend="trino")
    assert classify_sql_error(wrapper).code == 516
    assert is_non_retryable_sql_error(wrapper)


@pytest.mark.parametrize(
    "error",
    [
        NativeGpError("40001", "query contains 'permission denied'"),
        NativeGpError("P0001", "query contains 'syntax error'"),
        NativeClickHouseError(210, "query contains 'unknown table'"),
        NativeClickHouseError(210, "SELECT 'ssl: certificate_verify_failed'"),
    ],
)
def test_native_retry_decision_precedes_sql_text_in_wrapper(error) -> None:
    wrapper = RuntimeError(f"operation failed: {error}")
    wrapper.__cause__ = error
    assert classify_sql_error(wrapper).retryable is True


def test_metadata_only_proxy_error_keeps_unknown_type_retryable() -> None:
    error = RuntimeError("proxy included query text: SELECT 'permission denied'")
    error.error_type = "EXTERNAL"
    decision = classify_sql_error(error)
    assert decision.retryable is True
    assert decision.backend == "trino"


def test_nested_transport_diagnostics_keep_the_outer_native_code() -> None:
    error = NativeClickHouseError(210)
    error.__cause__ = NativeClickHouseError(209)
    decision = classify_sql_error(error)
    assert decision.retryable is True
    assert decision.code == 210


@pytest.mark.parametrize("error_name", ["SchemaNotFoundError", "TableNotFoundError"])
def test_legacy_missing_object_error_types_stop_without_native_metadata(error_name) -> None:
    error_class = type(error_name, (RuntimeError,), {})
    decision = classify_sql_error(error_class("neutral diagnostic"))
    assert decision.retryable is False
    assert decision.reason == "legacy_deterministic_error"


@pytest.mark.parametrize("attribute", ["name", "error_name"])
@pytest.mark.parametrize("name", ["FEATURE_NOT_SUPPORTED", "INSUFFICIENT_PRIVILEGE"])
def test_legacy_symbolic_aliases_remain_terminal(attribute, name) -> None:
    error = RuntimeError("neutral diagnostic")
    setattr(error, attribute, name)
    decision = classify_sql_error(error)
    assert decision.retryable is False
    assert decision.reason == "legacy_deterministic_error"


@pytest.mark.parametrize("attribute", ["name", "error_name"])
@pytest.mark.parametrize("name", ["FEATURE_NOT_SUPPORTED", "INSUFFICIENT_PRIVILEGE"])
@pytest.mark.parametrize(
    ("error_class", "code", "backend"),
    [(NativeGpError, "40001", "gp"), (NativeClickHouseError, 210, "ch")],
)
def test_native_transient_code_precedes_legacy_symbolic_alias(
    error_class, code, backend, attribute, name
) -> None:
    error = error_class(code)
    setattr(error, attribute, name)

    decision = classify_sql_error(error)

    assert decision.retryable is True
    assert decision.backend == backend
    assert decision.code == code


def test_structured_trino_name_precedes_legacy_compatibility_alias() -> None:
    error = RuntimeError("neutral diagnostic")
    error.error_type = "EXTERNAL"
    error.error_name = "FUTURE_CONNECTOR_FAILURE"
    error.name = "FEATURE_NOT_SUPPORTED"

    decision = classify_sql_error(error)

    assert decision.retryable is True
    assert decision.backend == "trino"
    assert decision.code == "FUTURE_CONNECTOR_FAILURE"


def test_stop_log_includes_stable_reason_and_code(monkeypatch, capsys) -> None:
    error = NativeClickHouseError(516)
    monkeypatch.setattr(
        "analytics_toolkit.sql.dml.transfer.runtime.retry.time.sleep", lambda _: None
    )

    def operation(_attempt: int) -> None:
        raise error

    with pytest.raises(NativeClickHouseError):
        run_with_retry("native failure", 3, 1, operation, safe_exception_logging=True)

    output = capsys.readouterr().out
    assert "reason=clickhouse_authentication" in output
    assert "code=516" in output
    assert "neutral diagnostic" not in output


def test_explicit_retry_exception_filter_and_custom_predicate_remain_supported() -> None:
    attempts: list[int] = []

    def operation(attempt: int) -> None:
        attempts.append(attempt)
        message = "unknown failure"
        raise ValueError(message)

    with pytest.raises(ValueError, match="unknown failure"):
        run_with_retry("filtered", 3, 0, operation, retryable_exceptions=(ConnectionError,))
    assert attempts == [1]
    attempts.clear()
    with pytest.raises(ValueError, match="unknown failure"):
        run_with_retry("custom", 3, 0, operation, non_retryable_predicate=lambda _: True)
    assert attempts == [1]
