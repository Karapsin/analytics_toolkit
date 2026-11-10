from __future__ import annotations

import re

# These errors require a change to the request, configuration, or recovery state.
TERMINAL_ERROR_CODES: dict[int, tuple[str, str]] = {
    6: ("CANNOT_PARSE_TEXT", "clickhouse_data"),
    20: ("NUMBER_OF_COLUMNS_DOESNT_MATCH", "clickhouse_data"),
    36: ("BAD_ARGUMENTS", "clickhouse_query"),
    43: ("ILLEGAL_TYPE_OF_ARGUMENT", "clickhouse_type"),
    46: ("UNKNOWN_FUNCTION", "clickhouse_query"),
    47: ("UNKNOWN_IDENTIFIER", "clickhouse_query"),
    48: ("NOT_IMPLEMENTED", "clickhouse_query"),
    50: ("UNKNOWN_TYPE", "clickhouse_type"),
    53: ("TYPE_MISMATCH", "clickhouse_type"),
    57: ("TABLE_ALREADY_EXISTS", "clickhouse_schema"),
    58: ("TABLE_METADATA_ALREADY_EXISTS", "clickhouse_schema"),
    60: ("UNKNOWN_TABLE", "clickhouse_schema"),
    62: ("SYNTAX_ERROR", "clickhouse_query"),
    63: ("UNKNOWN_AGGREGATE_FUNCTION", "clickhouse_query"),
    70: ("CANNOT_CONVERT_TYPE", "clickhouse_data"),
    80: ("INCORRECT_QUERY", "clickhouse_query"),
    81: ("UNKNOWN_DATABASE", "clickhouse_schema"),
    82: ("DATABASE_ALREADY_EXISTS", "clickhouse_schema"),
    115: ("UNKNOWN_SETTING", "clickhouse_configuration"),
    117: ("INCORRECT_DATA", "clickhouse_data"),
    159: ("TIMEOUT_EXCEEDED", "clickhouse_timeout"),
    160: ("TOO_SLOW", "clickhouse_timeout"),
    164: ("READONLY", "clickhouse_policy"),
    184: ("ILLEGAL_AGGREGATION", "clickhouse_query"),
    192: ("UNKNOWN_USER", "clickhouse_authentication"),
    193: ("WRONG_PASSWORD", "clickhouse_authentication"),
    194: ("REQUIRED_PASSWORD", "clickhouse_authentication"),
    195: ("IP_ADDRESS_NOT_ALLOWED", "clickhouse_access"),
    201: ("QUOTA_EXCEEDED", "clickhouse_quota"),
    216: ("QUERY_WITH_SAME_ID_IS_ALREADY_RUNNING", "clickhouse_ambiguous_outcome"),
    217: ("CLIENT_HAS_CONNECTED_TO_WRONG_PORT", "clickhouse_configuration"),
    229: ("QUERY_IS_TOO_LARGE", "clickhouse_query"),
    241: ("MEMORY_LIMIT_EXCEEDED", "clickhouse_memory"),
    243: ("NOT_ENOUGH_SPACE", "clickhouse_storage_capacity"),
    248: ("INVALID_PARTITION_VALUE", "clickhouse_data"),
    252: ("TOO_MANY_PARTS", "clickhouse_parts_limit"),
    291: ("DATABASE_ACCESS_DENIED", "clickhouse_access"),
    319: ("UNKNOWN_STATUS_OF_INSERT", "clickhouse_ambiguous_outcome"),
    321: ("VALUE_IS_OUT_OF_RANGE_OF_DATA_TYPE", "clickhouse_data"),
    349: ("CANNOT_INSERT_NULL_IN_ORDINARY_COLUMN", "clickhouse_data"),
    386: ("NO_COMMON_TYPE", "clickhouse_type"),
    392: ("QUERY_IS_PROHIBITED", "clickhouse_policy"),
    394: ("QUERY_WAS_CANCELLED", "clickhouse_cancelled"),
    407: ("DECIMAL_OVERFLOW", "clickhouse_data"),
    452: ("SETTING_CONSTRAINT_VIOLATION", "clickhouse_configuration"),
    471: ("INVALID_SETTING_VALUE", "clickhouse_configuration"),
    497: ("ACCESS_DENIED", "clickhouse_access"),
    516: ("AUTHENTICATION_FAILED", "clickhouse_authentication"),
}

_TRANSIENT_ERROR_CODES = {
    32: "ATTEMPT_TO_READ_AFTER_EOF",
    202: "TOO_MANY_SIMULTANEOUS_QUERIES",
    203: "NO_FREE_CONNECTION",
    209: "SOCKET_TIMEOUT",
    210: "NETWORK_ERROR",
    225: "NO_ZOOKEEPER",
    242: "TABLE_IS_READ_ONLY",
    254: "NO_ACTIVE_REPLICAS",
    265: "NO_AVAILABLE_REPLICA",
    279: "ALL_CONNECTION_TRIES_FAILED",
    285: "TOO_FEW_LIVE_REPLICAS",
    286: "UNSATISFIED_QUORUM_FOR_PREVIOUS_WRITE",
}
_CODES_BY_NAME = {
    **{name: code for code, (name, _reason) in TERMINAL_ERROR_CODES.items()},
    **{name: code for code, name in _TRANSIENT_ERROR_CODES.items()},
    # Older native-driver releases retain these historical names.
    "QUOTA_EXPIRED": 201,
    "TOO_LESS_LIVE_REPLICAS": 285,
}
_LEGACY_TERMINAL_NAMES = frozenset(
    {
        "ALREADY_EXISTS",
        "COLUMN_NOT_FOUND",
        "FEATURE_NOT_SUPPORTED",
        "FUNCTION_NOT_FOUND",
        "INSUFFICIENT_PRIVILEGE",
        "SCHEMA_NOT_FOUND",
        "TABLE_NOT_FOUND",
    }
)
_HTTP_CODE = re.compile(r"^\s*Received ClickHouse exception,\s*code:\s*(\d+)\b", re.IGNORECASE)
_HTTP_STATUS = re.compile(r"^\s*HTTP driver received HTTP status (\d{3})\b", re.IGNORECASE)
_LEGACY_HTTP_RESPONSE = re.compile(
    r"^\s*:?\s*HTTPDriver for https?://\S+ returned response code (\d{3})\)"
    r"(?:\r?\n[ \t]*(.*))?\Z",
    re.IGNORECASE | re.DOTALL,
)
_SERVER_CODE = re.compile(r"^\s*Code:\s*(\d+)\.\s*DB::Exception:", re.IGNORECASE)
_SERVER_NAME = re.compile(
    r"\(([A-Z][A-Z0-9_]*)\)(?:\s*\(version [^)]*\))?"
    r"(?:\s*\(for url [^)]*\))?\s*$",
    re.IGNORECASE,
)
_SYMBOLIC_NAME = re.compile(r"[A-Z][A-Z0-9_]*\Z")
_BARE_CONVERSION_DIAGNOSTIC = re.compile(
    r"^\s*(?:DB::Exception:\s*|Received ClickHouse exception(?::|,\s*server response:)"
    r"\s*(?:DB::Exception:\s*)?)(?:Attempt to read after eof:\s*)?Cannot (?:parse|convert)\b",
    re.IGNORECASE,
)
_CONVERSION_EOF_DIAGNOSTIC = re.compile(
    r"^\s*(?:Received ClickHouse exception,\s*code:\s*32,\s*server response:\s*)?"
    r"(?:Code:\s*32\.\s*)?DB::Exception:\s*"
    r"Attempt to read after eof:\s*Cannot (?:parse|convert)\b",
    re.IGNORECASE,
)
_NATIVE_CERTIFICATE_DIAGNOSTIC = re.compile(
    r"^\s*(?:Code:\s*210\.\s*)?(?:\[SSL:\s*CERTIFICATE_VERIFY_FAILED\]\s*)?"
    r"certificate verify failed\b",
    re.IGNORECASE,
)
_HTTP_TERMINAL_REASONS = {401: "clickhouse_authentication", 403: "clickhouse_access"}


def classify_error(exc: BaseException) -> tuple[bool, str, str | int | None] | None:
    """Classify one trusted ClickHouse exception; the caller traverses wrappers."""
    message = str(exc)
    if _is_native_certificate_error(exc, message):
        return False, "clickhouse_tls_certificate", 210
    structured = _classify_attributes(exc, message)
    if structured is not None:
        return structured
    return _classify_message(message)


def _classify_attributes(exc: BaseException, message: str) -> tuple[bool, str, str | int] | None:
    raw_code = getattr(exc, "code", None)
    if isinstance(raw_code, int) and not isinstance(raw_code, bool):
        return _classify_code(raw_code, message)
    if isinstance(raw_code, str) and raw_code.strip().isdigit():
        return _classify_code(int(raw_code.strip()), message)

    for attribute in ("error_name", "name"):
        name = getattr(exc, attribute, None)
        if isinstance(name, str):
            name = name.strip().upper()
            if _SYMBOLIC_NAME.fullmatch(name):
                return _classify_name(name, message)
    return None


def _classify_message(message: str) -> tuple[bool, str, str | int | None] | None:
    legacy = _LEGACY_HTTP_RESPONSE.match(message)
    if legacy is not None:
        body = _classify_server_diagnostic(legacy.group(2) or "")
        return body if body is not None else _classify_http_status(int(legacy.group(1)))
    server = _classify_server_diagnostic(message)
    if server is not None:
        return server
    status_match = _HTTP_STATUS.match(message)
    return _classify_http_status(int(status_match.group(1))) if status_match is not None else None


def _classify_http_status(status: int) -> tuple[bool, str, int]:
    reason = _HTTP_TERMINAL_REASONS.get(status)
    return reason is None, reason or "clickhouse_unclassified", status


def _classify_server_diagnostic(message: str) -> tuple[bool, str, str | int | None] | None:
    code_match = _HTTP_CODE.match(message) or _SERVER_CODE.match(message)
    if code_match is not None:
        return _classify_code(int(code_match.group(1)), message)
    if message.lstrip().lower().startswith(("db::exception:", "received clickhouse exception")):
        name_match = _SERVER_NAME.search(message)
        if name_match is not None:
            return _classify_name(name_match.group(1).upper(), message)
    if _BARE_CONVERSION_DIAGNOSTIC.match(message):
        return False, "clickhouse_data", None
    return None


def _is_native_certificate_error(exc: BaseException, message: str) -> bool:
    return any(
        cls.__module__ == "clickhouse_driver.errors" and cls.__name__ == "NetworkError"
        for cls in type(exc).__mro__
    ) and bool(_NATIVE_CERTIFICATE_DIAGNOSTIC.match(message))


def _classify_code(code: int, message: str) -> tuple[bool, str, int]:
    terminal = TERMINAL_ERROR_CODES.get(code)
    if terminal is not None:
        return False, terminal[1], code
    if code == _CODES_BY_NAME["ATTEMPT_TO_READ_AFTER_EOF"] and _CONVERSION_EOF_DIAGNOSTIC.match(
        message
    ):
        return False, "clickhouse_data", code
    if code in _TRANSIENT_ERROR_CODES:
        return True, "clickhouse_transient", code
    return True, "clickhouse_unclassified", code


def _classify_name(name: str, message: str) -> tuple[bool, str, str | int]:
    code = _CODES_BY_NAME.get(name)
    if code is not None:
        return _classify_code(code, message)
    if name in _LEGACY_TERMINAL_NAMES:
        return False, "legacy_deterministic_error", name
    return True, "clickhouse_unclassified", name
