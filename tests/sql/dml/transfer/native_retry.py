"""Native assignment data errors stop while transient SQL failures retain replay."""

from __future__ import annotations

import pytest
from analytics_toolkit.sql.dml.transfer.runtime.retry import is_non_retryable_sql_error
from psycopg2 import DataError, IntegrityError, OperationalError


@pytest.mark.parametrize("error_type", [DataError, IntegrityError])
def test_native_assignment_dbapi_data_and_integrity_failures_stop_without_sqlstate(error_type):
    assert is_non_retryable_sql_error(error_type("native assignment failed"))


@pytest.mark.parametrize(
    ("sqlstate", "deterministic"),
    [
        ("22001", True),
        ("22P02", True),
        ("23502", True),
        ("23514", True),
        ("40001", False),
        ("40P01", False),
        ("08006", False),
        ("22", False),
        ("23", False),
        ("", False),
    ],
)
def test_native_assignment_standard_sqlstates_preserve_transient_retry(sqlstate, deterministic):
    error = RuntimeError("native statement failed")
    error.sqlstate = sqlstate
    assert is_non_retryable_sql_error(error) is deterministic


def test_native_assignment_operational_error_without_sqlstate_stays_replayable():
    assert not is_non_retryable_sql_error(OperationalError("server acknowledgement was lost"))
