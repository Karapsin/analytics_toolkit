from __future__ import annotations

from typing import Any

from trino.exceptions import TrinoUserError

ClickHouseDatabaseError = type(
    "DatabaseError", (Exception,), {"__module__": "clickhouse_connect.driver.exceptions"}
)


def legacy_clickhouse_error(name: str) -> Exception:
    error = ClickHouseDatabaseError("private exception details")
    error.__dict__["name"] = name
    return error


def marked_trino_error(field: str, value: Any) -> TrinoUserError:
    payload: dict[str, Any] = {
        "message": "private exception details",
        "errorType": "INTERNAL_ERROR",
        "errorName": None,
        "errorCode": 1,
    }
    payload[field] = value
    error = TrinoUserError(payload)
    error.__dict__["analytics_toolkit_sql_retry_safe"] = False
    return error


def trusted_native_error(backend: str) -> Exception:
    if backend == "ch":
        return ClickHouseDatabaseError(
            "Code: 516. DB::Exception: private exception details. (AUTHENTICATION_FAILED)"
        )
    if backend == "trino":
        return TrinoUserError(
            {
                "message": "private exception details",
                "errorType": "USER_ERROR",
                "errorName": "TYPE_MISMATCH",
                "errorCode": 58,
            }
        )
    error = RuntimeError("private exception details")
    error.__dict__["pgcode"] = "22P02"
    return error
