"""Native Greenplum INET parameters preserve address host bits and masks."""

from __future__ import annotations

import os
import uuid
from contextlib import closing

import pytest
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.connection.get_sql_connection import get_sql_connection

from tests.sql.integration._support.backends import backend_enabled
from tests.sql.integration._support.identity import query_label

pytestmark = [pytest.mark.integration, pytest.mark.integration_core]


def _label() -> str:
    return query_label(
        os.environ.get("SQL_INTEGRATION_RUN_ID", uuid.uuid4().hex[:8]),
        os.environ.get("SQL_INTEGRATION_TEST_ID", "manual"),
        "gp-inet-parameter-cast",
    )


def _cases() -> list[tuple[str | None, str, str | None, int | None]]:
    return [
        ("192.0.2.1/24", "INET '192.0.2.1/24'", "192.0.2.1", 24),
        (
            "2001:0DB8:0000:0000:0000:0000:0000:0001/64",
            "INET '2001:db8::1/64'",
            "2001:db8::1",
            64,
        ),
        ("192.0.2.1", "INET '192.0.2.1/32'", "192.0.2.1", 32),
        (None, "CAST(NULL AS INET)", None, None),
    ]


@pytest.mark.sql_scenario("audit.load.gp_inet_parameters")
def test_greenplum_inet_parameters_preserve_native_hosts_and_masks() -> None:
    if not backend_enabled("gp"):
        pytest.skip("Greenplum requires x86_64")
    cases = _cases()
    casts, results = [], []
    for index, (_value, native, _host, _mask) in enumerate(cases):
        casts.extend([f"CAST(%s AS INET) AS actual_{index}", f"{native} AS native_{index}"])
        results.extend(
            [
                f"actual_{index}::text",
                f"native_{index}::text",
                f"actual_{index} IS NOT DISTINCT FROM native_{index}",
                f"host(actual_{index})",
                f"masklen(actual_{index})",
            ]
        )
    query = (
        f"/* {_label()} */ WITH casts AS (SELECT "
        + ", ".join(casts)
        + ") SELECT "
        + ", ".join(results)
        + " FROM casts"
    )
    normalized = get_backend_adapter("gp").normalize_insert_rows(
        [(value,) for value, _native, _host, _mask in cases]
    )
    parameters = [row[0] for row in normalized]
    with closing(get_sql_connection("gp_source")) as connection, closing(
        connection.cursor()
    ) as cursor:
        cursor.execute(query, parameters)
        rows = cursor.fetchall()
    assert len(rows) == 1
    assert len(rows[0]) == len(cases) * 5
    for index, (_value, _native, expected_host, expected_mask) in enumerate(cases):
        actual, native, equal, host, mask = rows[0][index * 5 : index * 5 + 5]
        # Delegate native INET text formatting to the independent server control;
        # host() and masklen() separately certify retained host bits and prefix.
        assert actual == native
        assert equal is True
        assert host == expected_host
        assert mask == expected_mask
        if expected_host is None:
            assert actual is None
        else:
            assert isinstance(actual, str)
