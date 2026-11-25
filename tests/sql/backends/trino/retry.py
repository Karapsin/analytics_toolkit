from __future__ import annotations

from types import SimpleNamespace

import pytest
from analytics_toolkit.sql.backends.trino.retry import classify_error
from trino.exceptions import HttpError, TrinoAuthError, TrinoQueryError, TrinoUserError


@pytest.mark.parametrize(
    ("error_name", "reason"),
    [
        ("SYNTAX_ERROR", "trino_invalid_query"),
        ("CATALOG_NOT_FOUND", "trino_invalid_query"),
        ("SCHEMA_NOT_FOUND", "trino_invalid_query"),
        ("TABLE_NOT_FOUND", "trino_invalid_query"),
        ("COLUMN_NOT_FOUND", "trino_invalid_query"),
        ("FUNCTION_NOT_FOUND", "trino_invalid_query"),
        ("ALREADY_EXISTS", "trino_invalid_query"),
        ("TYPE_MISMATCH", "trino_invalid_query"),
        ("INVALID_CAST_ARGUMENT", "trino_invalid_query"),
        ("INVALID_FUNCTION_ARGUMENT", "trino_invalid_query"),
        ("DIVISION_BY_ZERO", "trino_invalid_query"),
        ("NUMERIC_VALUE_OUT_OF_RANGE", "trino_invalid_query"),
        ("PERMISSION_DENIED", "trino_invalid_query"),
        ("CONSTRAINT_VIOLATION", "trino_invalid_query"),
        ("NOT_SUPPORTED", "trino_invalid_query"),
        ("INVALID_SESSION_PROPERTY", "trino_invalid_query"),
        ("INVALID_TABLE_PROPERTY", "trino_invalid_query"),
        ("CONFIGURATION_INVALID", "trino_invalid_query"),
        ("COMPILER_ERROR", "trino_invalid_query"),
        ("QUERY_EXCEEDED_COMPILER_LIMIT", "trino_invalid_query"),
        ("USER_CANCELED", "trino_cancelled"),
        ("ADMINISTRATIVELY_KILLED", "trino_cancelled"),
        ("EXCEEDED_GLOBAL_MEMORY_LIMIT", "trino_fixed_limit"),
        ("EXCEEDED_LOCAL_MEMORY_LIMIT", "trino_fixed_limit"),
        ("EXCEEDED_FUNCTION_MEMORY_LIMIT", "trino_fixed_limit"),
        ("EXCEEDED_TIME_LIMIT", "trino_fixed_limit"),
        ("EXCEEDED_CPU_LIMIT", "trino_fixed_limit"),
        ("EXCEEDED_SPILL_LIMIT", "trino_fixed_limit"),
        ("EXCEEDED_SCAN_LIMIT", "trino_fixed_limit"),
        ("EXCEEDED_WRITE_LIMIT", "trino_fixed_limit"),
        ("EXCEEDED_OUTPUT_LIMIT", "trino_fixed_limit"),
        ("HIVE_BAD_DATA", "trino_invalid_connector_data"),
        ("HIVE_PARTITION_SCHEMA_MISMATCH", "trino_invalid_connector_data"),
        ("HIVE_INVALID_METADATA", "trino_invalid_connector_data"),
        ("HIVE_UNSUPPORTED_FORMAT", "trino_invalid_connector_data"),
        ("ICEBERG_BAD_DATA", "trino_invalid_connector_data"),
        ("ICEBERG_INVALID_METADATA", "trino_invalid_connector_data"),
        ("ICEBERG_INVALID_PARTITION_VALUE", "trino_invalid_connector_data"),
    ],
)
def test_terminal_native_names_do_not_require_matching_message(
    error_name: str, reason: str
) -> None:
    error = TrinoQueryError({"errorName": error_name, "message": "request failed"})

    assert classify_error(error) == (False, reason, error_name)


def test_trino_user_error_uses_public_metadata_properties() -> None:
    error = TrinoUserError(
        {
            "errorName": "INVALID_CAST_ARGUMENT",
            "errorType": "USER_ERROR",
            "errorCode": 9,
            "message": "request failed",
        },
        query_id="query-1",
    )

    assert classify_error(error) == (False, "trino_invalid_query", "INVALID_CAST_ARGUMENT")


def test_dictionary_arguments_support_minimal_native_payload() -> None:
    error = Exception({"errorName": " not_supported ", "message": "request failed"})

    assert classify_error(error) == (False, "trino_invalid_query", "NOT_SUPPORTED")


def test_attribute_only_error_retains_legacy_test_double_support() -> None:
    error = Exception("request failed")
    error.error_name = "TYPE_MISMATCH"

    assert classify_error(error) == (False, "trino_invalid_query", "TYPE_MISMATCH")


def test_attribute_only_numeric_metadata_preserves_zero_code() -> None:
    error = Exception("request failed")
    error.error_type = "INTERNAL_ERROR"
    error.error_code = 0

    assert classify_error(error) == (True, "trino_legacy_retry", 0)


@pytest.mark.parametrize(
    ("error_name", "error_type"),
    [
        ("TRANSACTION_CONFLICT", "USER_ERROR"),
        ("QUERY_QUEUE_FULL", "INSUFFICIENT_RESOURCES"),
        ("CLUSTER_OUT_OF_MEMORY", "INSUFFICIENT_RESOURCES"),
        ("SERVER_SHUTTING_DOWN", "INTERNAL_ERROR"),
        ("SERVER_STARTING_UP", "INTERNAL_ERROR"),
        ("REMOTE_HOST_GONE", "INTERNAL_ERROR"),
        ("PAGE_TRANSPORT_TIMEOUT", "INTERNAL_ERROR"),
        ("HIVE_METASTORE_ERROR", "EXTERNAL"),
        ("ICEBERG_COMMIT_ERROR", "EXTERNAL"),
        ("FUTURE_CONNECTOR_FAILURE", "FUTURE_ERROR_TYPE"),
    ],
)
def test_native_non_catalog_errors_preserve_retry_despite_misleading_text(
    error_name: str, error_type: str
) -> None:
    error = TrinoQueryError(
        {
            "errorName": error_name,
            "errorType": error_type,
            "message": "remote query contained syntax error text",
        }
    )

    assert classify_error(error) == (True, "trino_legacy_retry", error_name)


@pytest.mark.parametrize("payload", [{}, {"errorType": "EXTERNAL", "errorCode": 12345}])
def test_native_query_without_name_remains_retryable(payload: dict[str, object]) -> None:
    error = TrinoQueryError(payload)

    assert classify_error(error) == (True, "trino_legacy_retry", payload.get("errorCode"))


@pytest.mark.parametrize(
    "error",
    [Exception("syntax error"), Exception({"message": "syntax error"}), ValueError("failure")],
)
def test_non_native_errors_leave_legacy_fallback_available(error: BaseException) -> None:
    assert classify_error(error) is None


def test_trino_authentication_error_stops() -> None:
    assert classify_error(TrinoAuthError("request failed")) == (
        False,
        "trino_authentication",
        None,
    )


def test_unrelated_http_error_class_does_not_acquire_trino_policy() -> None:
    class HttpError(Exception):
        pass

    assert classify_error(HttpError("error 401: request failed")) is None


@pytest.mark.parametrize("status", [401, 403])
def test_persistent_http_authentication_status_stops(status: int) -> None:
    error = HttpError(f"error {status}: request failed")

    assert classify_error(error) == (False, "trino_authentication", status)


@pytest.mark.parametrize("status", [429, 502, 503, 504])
def test_transient_http_status_keeps_legacy_retry(status: int) -> None:
    error = HttpError(f"error {status}: syntax error text from proxy")

    assert classify_error(error) == (True, "trino_legacy_retry", status)


@pytest.mark.parametrize(
    ("method", "url", "expected"),
    [
        ("POST", "https://coordinator/v1/statement", (False, "trino_invalid_endpoint", 404)),
        ("POST", "https://coordinator/v1/statement/", (False, "trino_invalid_endpoint", 404)),
        ("GET", "https://coordinator/v1/statement", None),
        ("GET", "https://coordinator/v1/statement/executing/query-1/1", None),
        ("POST", "https://coordinator/unknown", None),
        ("POST", "http://[invalid/v1/statement", None),
        (None, "https://coordinator/v1/statement", None),
        ("POST", None, None),
    ],
)
def test_http_not_found_requires_submit_endpoint_proof(
    method: str | None,
    url: str | None,
    expected: tuple[bool, str, int] | None,
) -> None:
    error = HttpError("request failed")
    error.response = SimpleNamespace(
        status_code=404, request=SimpleNamespace(method=method, url=url)
    )

    assert classify_error(error) == expected


def test_plain_http_not_found_does_not_prove_invalid_submit_endpoint() -> None:
    assert classify_error(HttpError("error 404: request failed")) is None


def test_http_error_request_metadata_can_be_attached_directly() -> None:
    error = HttpError("error 404: request failed")
    error.request = SimpleNamespace(method="POST", url="https://coordinator/v1/statement")

    assert classify_error(error) == (False, "trino_invalid_endpoint", 404)


def test_http_error_status_metadata_can_be_attached_directly() -> None:
    error = HttpError("request failed")
    error.status_code = 403

    assert classify_error(error) == (False, "trino_authentication", 403)


@pytest.mark.parametrize("arguments", [(), (None,), ("request failed",), ("error 500: failure",)])
def test_other_http_errors_leave_existing_fallback_available(arguments: tuple[object, ...]) -> None:
    assert classify_error(HttpError(*arguments)) is None


def test_malformed_optional_driver_metadata_does_not_raise() -> None:
    error = TrinoQueryError("request failed")

    assert classify_error(error) == (True, "trino_legacy_retry", None)


@pytest.mark.parametrize("metadata_error", [AttributeError, TypeError])
def test_native_payload_survives_broken_sdk_error_name(
    metadata_error: type[Exception],
) -> None:
    class MalformedQueryError(TrinoQueryError):
        @property
        def error_name(self) -> str | None:
            message = "malformed native error name"
            raise metadata_error(message)

    payload = {"errorName": "INVALID_CAST_ARGUMENT", "message": "request failed"}
    error = MalformedQueryError(payload, query_id="query-1")

    assert classify_error(error) == (False, "trino_invalid_query", "INVALID_CAST_ARGUMENT")
    assert error.args == (payload,)
    assert error.query_id == "query-1"


def test_argument_payload_survives_broken_private_sdk_metadata() -> None:
    class MalformedQueryError(Exception):
        @property
        def _error(self) -> dict[str, object]:
            message = "malformed private native metadata"
            raise TypeError(message)

    payload = {"errorName": "NOT_SUPPORTED", "message": "request failed"}
    error = MalformedQueryError(payload)

    assert classify_error(error) == (False, "trino_invalid_query", "NOT_SUPPORTED")
    assert error.args == (payload,)


def test_invalid_native_code_is_not_exposed_as_classification_code() -> None:
    error = Exception({"errorType": "EXTERNAL", "errorCode": {"invalid": "value"}})

    assert classify_error(error) == (True, "trino_legacy_retry", None)
