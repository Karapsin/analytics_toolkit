from __future__ import annotations

from typing import Any

import pytest
from analytics_toolkit.sql.backends.ch.retry import classify_error

# Importing the SDK exceptions also loads its unrelated C extensions. Match the
# exception identity and hierarchy here without importing that transport stack.
ClickHouseDatabaseError = type(
    "DatabaseError", (Exception,), {"__module__": "clickhouse_connect.driver.exceptions"}
)
ClickHouseOperationalError = type(
    "OperationalError",
    (ClickHouseDatabaseError,),
    {"__module__": "clickhouse_connect.driver.exceptions"},
)

_NATIVE_NETWORK_ERROR = type(
    "NetworkError", (RuntimeError,), {"__module__": "clickhouse_driver.errors", "code": 210}
)


@pytest.mark.parametrize(
    ("codes", "reason"),
    [
        ([6, 20, 70, 117, 248, 321, 349, 407], "clickhouse_data"),
        ([36, 46, 47, 48, 62, 63, 80, 184, 229], "clickhouse_query"),
        ([43, 50, 53, 386], "clickhouse_type"),
        ([57, 58, 60, 81, 82], "clickhouse_schema"),
        ([115, 217, 452, 471], "clickhouse_configuration"),
        ([159, 160], "clickhouse_timeout"),
        ([164, 392], "clickhouse_policy"),
        ([192, 193, 194, 516], "clickhouse_authentication"),
        ([195, 291, 497], "clickhouse_access"),
        ([201], "clickhouse_quota"),
        ([216, 319], "clickhouse_ambiguous_outcome"),
        ([241], "clickhouse_memory"),
        ([243], "clickhouse_storage_capacity"),
        ([252], "clickhouse_parts_limit"),
        ([394], "clickhouse_cancelled"),
    ],
)
def test_terminal_native_and_http_error_codes(codes: list[int], reason: str) -> None:
    for code in codes:
        native = RuntimeError("The same query and input cannot recover automatically.")
        native.code = code  # type: ignore[attr-defined]
        http = RuntimeError(
            f"Received ClickHouse exception, code: {code}, server response: "
            f"Code: {code}. DB::Exception: Request rejected. (for url http://localhost:8123)"
        )
        legacy_http = ClickHouseDatabaseError(
            ":HTTPDriver for https://localhost:8443 returned response code 500)\n "
            f"Code: {code}. DB::Exception: Request rejected."
        )

        assert classify_error(native) == (False, reason, code)
        assert classify_error(http) == (False, reason, code)
        assert classify_error(legacy_http) == (False, reason, code)


@pytest.mark.parametrize("code", [32, 202, 203, 209, 210, 225, 242, 254, 265, 279, 285, 286])
def test_transient_codes_preserve_retries(code: int) -> None:
    error = RuntimeError(
        f"Received ClickHouse exception, code: {code}, server response: "
        "DB::Exception: Query contains SELECT 'SYNTAX_ERROR'."
    )

    assert classify_error(error) == (True, "clickhouse_transient", code)


@pytest.mark.parametrize(
    ("diagnostic", "retryable", "reason"),
    [
        (
            "Attempt to read after eof: Cannot parse Int64 from String, because value is "
            "too short: while executing 'FUNCTION CAST(customer_id, Int64)'.",
            False,
            "clickhouse_data",
        ),
        (
            "Attempt to read after eof: Cannot convert String to Int64.",
            False,
            "clickhouse_data",
        ),
        ("Attempt to read after eof.", True, "clickhouse_transient"),
        (
            "Attempt to read after eof: while executing SELECT 'Cannot parse Int64 from String'.",
            True,
            "clickhouse_transient",
        ),
        (
            "Attempt to read after eof: 'Cannot parse Int64 from String' is SQL text.",
            True,
            "clickhouse_transient",
        ),
    ],
)
def test_conversion_eof_stops_without_treating_transport_eof_as_bad_data(
    diagnostic: str, retryable: bool, reason: str
) -> None:
    native = RuntimeError(f"Code: 32.\nDB::Exception: {diagnostic} (ATTEMPT_TO_READ_AFTER_EOF)")
    native.code = 32  # type: ignore[attr-defined]
    string_code = RuntimeError(f"DB::Exception: {diagnostic} (ATTEMPT_TO_READ_AFTER_EOF)")
    string_code.code = "32"  # type: ignore[attr-defined]
    symbolic = RuntimeError(f"DB::Exception: {diagnostic} (ATTEMPT_TO_READ_AFTER_EOF)")
    symbolic.error_name = "ATTEMPT_TO_READ_AFTER_EOF"  # type: ignore[attr-defined]
    http = RuntimeError(
        "Received ClickHouse exception, code: 32, server response: Code: 32. "
        f"DB::Exception: {diagnostic} (ATTEMPT_TO_READ_AFTER_EOF)"
    )
    legacy_http = ClickHouseDatabaseError(
        ":HTTPDriver for http://localhost:8123 returned response code 500)\n "
        f"Code: 32. DB::Exception: {diagnostic} (ATTEMPT_TO_READ_AFTER_EOF)"
    )

    for error in (native, string_code, symbolic, http, legacy_http):
        assert classify_error(error) == (retryable, reason, 32)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            RuntimeError("DB::Exception: Unknown function. (UNKNOWN_FUNCTION)"),
            (False, "clickhouse_query", 46),
        ),
        (
            RuntimeError(
                "DB::Exception: Invalid input. (INCORRECT_DATA) (version 26.1.1.1) "
                "(for url http://localhost:8123)"
            ),
            (False, "clickhouse_data", 117),
        ),
        (
            RuntimeError("Code: 497. DB::Exception: Access denied."),
            (False, "clickhouse_access", 497),
        ),
        (
            RuntimeError(
                "Received ClickHouse exception: Too few replicas. (TOO_LESS_LIVE_REPLICAS)"
            ),
            (True, "clickhouse_transient", 285),
        ),
        (
            RuntimeError("DB::Exception: Quota reached. (QUOTA_EXPIRED)"),
            (False, "clickhouse_quota", 201),
        ),
        (
            RuntimeError("DB::Exception: Future error. (FUTURE_ERROR)"),
            (True, "clickhouse_unclassified", "FUTURE_ERROR"),
        ),
        (
            RuntimeError("Received ClickHouse exception, code: 999999, SYNTAX_ERROR in SQL text"),
            (True, "clickhouse_unclassified", 999999),
        ),
    ],
)
def test_supported_server_diagnostic_shapes(
    error: Exception, expected: tuple[bool, str, str | int]
) -> None:
    assert classify_error(error) == expected


@pytest.mark.parametrize("attribute", ["error_name", "name"])
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("AUTHENTICATION_FAILED", (False, "clickhouse_authentication", 516)),
        ("SOCKET_TIMEOUT", (True, "clickhouse_transient", 209)),
        ("FUTURE_ERROR", (True, "clickhouse_unclassified", "FUTURE_ERROR")),
    ],
)
def test_symbolic_attributes(
    attribute: str, name: str, expected: tuple[bool, str, str | int]
) -> None:
    error = RuntimeError("Server rejected request.")
    setattr(error, attribute, name)

    assert classify_error(error) == expected


@pytest.mark.parametrize("code", [999999, " 999999 ", -1])
def test_unknown_native_code_overrides_misleading_query_text(code: int | str) -> None:
    error = RuntimeError("SQL text: SELECT 'SYNTAX_ERROR', 'permission denied'")
    error.code = code  # type: ignore[attr-defined]

    assert classify_error(error) == (True, "clickhouse_unclassified", int(code))


@pytest.mark.parametrize(
    ("code", "name", "message"),
    [
        (None, None, "temporary failure"),
        (True, None, "temporary failure"),
        (object(), "not a symbolic code", "temporary failure"),
        ("bad code", None, "Unknown function in an unrelated service"),
        (None, None, "SQL text: SELECT 'Code: 516. DB::Exception: (AUTHENTICATION_FAILED)'"),
        (None, None, "SELECT '(UNKNOWN_FUNCTION)'"),
        (None, None, "DB::Exception: No symbolic diagnostic is available."),
        (None, None, "DB::Exception: SELECT '(UNKNOWN_FUNCTION)'"),
    ],
)
def test_unrecognized_errors_leave_fallback_to_coordinator(
    code: Any, name: str | None, message: str
) -> None:
    error = RuntimeError(message)
    error.code = code  # type: ignore[attr-defined]
    error.name = name  # type: ignore[attr-defined]

    assert classify_error(error) is None


@pytest.mark.parametrize("error_type", [ClickHouseDatabaseError, ClickHouseOperationalError])
@pytest.mark.parametrize(
    ("status", "reason"), [(401, "clickhouse_authentication"), (403, "clickhouse_access")]
)
def test_http_gateway_authentication_errors_stop(
    error_type: type[Exception], status: int, reason: str
) -> None:
    error = error_type(
        f"HTTP driver received HTTP status {status}, server response: "
        "Gateway rejected credentials (for url https://localhost:8443)"
    )

    assert classify_error(error) == (False, reason, status)


@pytest.mark.parametrize("status", [400, 404, 429, 500, 502, 503, 504, 599])
def test_other_http_gateway_statuses_preserve_legacy_retry(status: int) -> None:
    error = ClickHouseOperationalError(
        f"HTTP driver received HTTP status {status}, server response: "
        "SQL text SELECT 'permission denied', 'AUTHENTICATION_FAILED'"
    )

    assert classify_error(error) == (True, "clickhouse_unclassified", status)


@pytest.mark.parametrize(
    "message",
    [
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
        "self-signed certificate (_ssl.c:1006) (localhost:9440)",
        "Code: 210. [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
        "certificate has expired (_ssl.c:1006) (localhost:9440)",
        "certificate verify failed: Hostname mismatch (localhost:9440)",
    ],
)
def test_native_transport_certificate_diagnostic_stops(message: str) -> None:
    error = _NATIVE_NETWORK_ERROR(message)

    assert classify_error(error) == (False, "clickhouse_tls_certificate", 210)


def test_native_network_error_subclasses_keep_certificate_classification() -> None:
    subclass = type("WrappedNetworkError", (_NATIVE_NETWORK_ERROR,), {})
    error = subclass("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unknown CA")

    assert classify_error(error) == (False, "clickhouse_tls_certificate", 210)


@pytest.mark.parametrize(
    "message",
    [
        "DB::Exception: Query SELECT '[SSL: CERTIFICATE_VERIFY_FAILED] "
        "certificate verify failed' failed remotely.",
        "Connection reset. SQL text: ssl: certificate_verify_failed",
    ],
)
def test_native_network_sql_text_does_not_become_a_certificate_failure(message: str) -> None:
    error = _NATIVE_NETWORK_ERROR(message)

    assert classify_error(error) == (True, "clickhouse_transient", 210)


@pytest.mark.parametrize(
    "message",
    [
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unknown CA",
        "Code: 210. DB::Exception: SELECT 'ssl: certificate_verify_failed'",
    ],
)
def test_native_server_error_code_does_not_establish_transport_certificate_identity(
    message: str,
) -> None:
    server_exception = type(
        "ServerException", (RuntimeError,), {"__module__": "clickhouse_driver.errors", "code": 210}
    )

    assert classify_error(server_exception(message)) == (True, "clickhouse_transient", 210)


@pytest.mark.parametrize(
    "message",
    [
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unknown CA",
        "SQL text: HTTP driver received HTTP status 401",
        "DB::Exception: SELECT 'HTTP driver received HTTP status 403'",
    ],
)
def test_unbranded_errors_cannot_claim_gateway_or_native_tls_status(message: str) -> None:
    assert classify_error(RuntimeError(message)) is None


@pytest.mark.parametrize("error_type", [ClickHouseDatabaseError, ClickHouseOperationalError])
@pytest.mark.parametrize(
    ("status", "reason"), [(401, "clickhouse_authentication"), (403, "clickhouse_access")]
)
@pytest.mark.parametrize("body", ["", "Gateway rejected the credentials."])
def test_minimum_sdk_http_gateway_authentication_shapes(
    error_type: type[Exception], status: int, reason: str, body: str
) -> None:
    message = f"HTTPDriver for https://localhost:8443 returned response code {status})"
    if body:
        message = f":{message}\n {body}"

    assert classify_error(error_type(message)) == (False, reason, status)


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (
            503,
            "Code: 516. DB::Exception: Authentication failed. (AUTHENTICATION_FAILED)",
            (False, "clickhouse_authentication", 516),
        ),
        (
            500,
            "Code: 210. DB::Exception: Query SELECT 'ssl: certificate_verify_failed', "
            "'AUTHENTICATION_FAILED' failed remotely.",
            (True, "clickhouse_transient", 210),
        ),
        (
            500,
            "Code: 999999. DB::Exception: SQL includes 'permission denied'.",
            (True, "clickhouse_unclassified", 999999),
        ),
        (
            500,
            "SELECT 'Code: 516. DB::Exception: Authentication failed'.",
            (True, "clickhouse_unclassified", 500),
        ),
        (
            500,
            "DB::Exception: Unknown function. (UNKNOWN_FUNCTION)",
            (False, "clickhouse_query", 46),
        ),
    ],
)
def test_minimum_sdk_body_diagnostic_precedes_http_status(
    status: int, body: str, expected: tuple[bool, str, int]
) -> None:
    error = ClickHouseDatabaseError(
        f":HTTPDriver for https://localhost:8443 returned response code {status})\n {body}"
    )

    assert classify_error(error) == expected


@pytest.mark.parametrize("status", [429, 500, 503, 504])
@pytest.mark.parametrize("body", ["", "SELECT 'AUTHENTICATION_FAILED'"])
def test_minimum_sdk_http_retry_statuses_keep_legacy_behavior(status: int, body: str) -> None:
    message = f"HTTPDriver for http://localhost:8123 returned response code {status})"
    if body:
        message = f":{message}\n {body}"

    assert classify_error(ClickHouseOperationalError(message)) == (
        True,
        "clickhouse_unclassified",
        status,
    )


@pytest.mark.parametrize(
    "message",
    [
        "SQL text: :HTTPDriver for https://localhost:8443 returned response code 401)",
        "DB::Exception: SELECT 'HTTPDriver for https://localhost:8443 returned response code 403)'",
        ":HTTPDriver for https://localhost:8443 returned response code 500) "
        "Code: 516. DB::Exception: Authentication failed.",
    ],
)
def test_minimum_sdk_brand_cannot_be_read_from_query_text(message: str) -> None:
    assert classify_error(ClickHouseDatabaseError(message)) is None
