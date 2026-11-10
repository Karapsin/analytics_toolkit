from __future__ import annotations

from types import SimpleNamespace

import pytest
from analytics_toolkit.sql.execution.retry_errors import classify_sql_error
from trino.exceptions import TrinoQueryError


@pytest.mark.parametrize("attribute", ["name", "error_name"])
@pytest.mark.parametrize("name", ["TYPE_MISMATCH", "SYNTAX_ERROR"])
@pytest.mark.parametrize("origin", ["marker", "argument"])
@pytest.mark.parametrize(("code", "retryable"), [(210, True), (516, False)])
def test_recorded_clickhouse_origin_precedes_shared_symbolic_name(
    attribute: str, name: str, origin: str, code: int, retryable: bool
) -> None:
    error = RuntimeError("neutral diagnostic")
    error.code = code
    setattr(error, attribute, name)
    if origin == "marker":
        error.analytics_toolkit_sql_backend = "ch"
        # A local source marker is stronger than a transfer destination hint.
        decision = classify_sql_error(error, backend="trino")
    else:
        decision = classify_sql_error(error, backend="ch")

    assert decision.retryable is retryable
    assert decision.backend == "ch"
    assert decision.code == code


@pytest.mark.parametrize("attribute", ["name", "error_name"])
def test_inherited_source_origin_precedes_shared_symbolic_name(attribute: str) -> None:
    source_error = RuntimeError("neutral diagnostic")
    source_error.code = 210
    setattr(source_error, attribute, "TYPE_MISMATCH")
    wrapper = RuntimeError("source stream failed")
    wrapper.analytics_toolkit_sql_backend = "ch"
    wrapper.sql_context = SimpleNamespace(backend="trino")
    wrapper.__cause__ = source_error

    decision = classify_sql_error(wrapper, backend="trino")

    assert decision.retryable is True
    assert decision.backend == "ch"
    assert decision.code == 210


def test_real_trino_sdk_identity_precedes_conflicting_clickhouse_origin() -> None:
    error = TrinoQueryError(
        {
            "errorName": "PAGE_TRANSPORT_TIMEOUT",
            "errorType": "INTERNAL_ERROR",
            "errorCode": 65540,
            "message": "neutral diagnostic",
        }
    )
    error.analytics_toolkit_sql_backend = "ch"
    error.name = "TYPE_MISMATCH"
    error.code = 516

    decision = classify_sql_error(error, backend="gp")

    assert decision.retryable is True
    assert decision.backend == "trino"
    assert decision.code == "PAGE_TRANSPORT_TIMEOUT"


@pytest.mark.parametrize(("sqlstate", "retryable"), [("22P02", False), ("40001", True)])
@pytest.mark.parametrize("attribute", ["name", "error_name"])
def test_native_sqlstate_precedes_conflicting_clickhouse_origin(
    sqlstate: str, retryable: bool, attribute: str
) -> None:
    error = RuntimeError("neutral diagnostic")
    error.pgcode = sqlstate
    error.analytics_toolkit_sql_backend = "ch"
    error.code = 516
    setattr(error, attribute, "TYPE_MISMATCH")

    decision = classify_sql_error(error, backend="trino")

    assert decision.retryable is retryable
    assert decision.backend == "gp"
    assert decision.code == sqlstate
