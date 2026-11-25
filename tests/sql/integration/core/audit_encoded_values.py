"""Disposable checks of final structured-text values against authoritative bounds."""

from __future__ import annotations

import os
import uuid
from typing import TYPE_CHECKING

import pytest
import sqlparse
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter

from tests.sql.integration._support.backends import (
    backend_alias,
    backend_enabled,
    integration_table,
)
from tests.sql.integration._support.identity import query_label
from tests.sql.integration.manifest import scenario_param

if TYPE_CHECKING:
    from typing import Any

    from tests.sql.integration._support.resources import ResourceRegistry

pytestmark = [pytest.mark.integration, pytest.mark.integration_core]

_ENCODED_EMPTY_ARRAY = '{"v":1,"backend":"ch","type":"Array(Int64)","value":["array",[]]}'


def _label(purpose: str) -> str:
    return query_label(
        os.environ.get("SQL_INTEGRATION_RUN_ID", uuid.uuid4().hex[:8]),
        os.environ.get("SQL_INTEGRATION_TEST_ID", "manual"),
        purpose,
    )


@pytest.mark.parametrize(
    "backend",
    [
        scenario_param(f"audit.transfer.encoded_varchar.{backend}", backend)
        for backend in ("gp", "trino")
    ],
)
def test_complete_structured_encoding_is_validated_before_creating_a_bounded_target(
    backend: str,
    monkeypatch: pytest.MonkeyPatch,
    resource_registry: ResourceRegistry,
) -> None:
    if not backend_enabled(backend):
        pytest.skip("Greenplum requires x86_64")
    source_alias, target_alias = "ch_source", backend_alias(backend, target=True)
    source = resource_registry.table(source_alias, integration_table("ch", "encoded_source"))
    rejected = resource_registry.table(target_alias, integration_table(backend, "encoded_reject"))
    fitting = resource_registry.table(target_alias, integration_table(backend, "encoded_fit"))
    sql.execute(
        source_alias,
        f"CREATE TABLE {source} (row_id Int64, items Array(Int64), "
        "nullable_marker Nullable(String)) ENGINE=MergeTree ORDER BY row_id",
        retry_cnt=1,
        query_label=_label("encoded-create"),
    )
    sql.execute(
        source_alias,
        f"INSERT INTO {source} VALUES (1, [], NULL), (2, [], '')",
        retry_cnt=1,
        query_label=_label("encoded-seed"),
    )
    assert len(_ENCODED_EMPTY_ARRAY) == 65
    assert not sql.table_info(target_alias, rejected).exists
    assert not sql.table_info(target_alias, fitting).exists
    adapter = get_backend_adapter(backend)
    execute_command = adapter.execute_command
    submitted_creates: list[str] = []

    def record_command(connection: Any, statement: str) -> Any:
        if any(parsed.get_type() == "CREATE" for parsed in sqlparse.parse(statement)):
            submitted_creates.append(statement)
        return execute_command(connection, statement)

    common = {
        "from_sql": f"SELECT row_id, items, nullable_marker FROM {source}",
        "write_mode": "replace",
        "retry_cnt": 1,
        "full_retry_cnt": 1,
        "concurrency": 1,
        "adaptive_batch_size": False,
        "target_rows_per_second": False,
    }
    if backend == "gp":
        common["gp_distributed_by_key"] = "row_id"
    schema = {"row_id": "BIGINT", "items": "VARCHAR(60)", "nullable_marker": "VARCHAR(1)"}
    with monkeypatch.context() as target_commands:
        # Observe real command submissions; successful commands still reach the
        # backend. Cleanup cannot hide a target that was created before validation.
        target_commands.setattr(adapter, "execute_command", record_command)
        with pytest.raises(
            ValueError,
            match=r"^Selected input cannot be converted to the resolved destination schema\.$",
        ):
            sql.transfer(
                source_alias,
                target_alias,
                to_table=rejected,
                table_schema=schema,
                query_label=_label("encoded-too-wide"),
                **common,
            )
    assert submitted_creates == []
    assert not sql.table_info(target_alias, rejected).exists

    with monkeypatch.context() as target_commands:
        target_commands.setattr(adapter, "execute_command", record_command)
        assert (
            sql.transfer(
                source_alias,
                target_alias,
                to_table=fitting,
                table_schema={**schema, "items": "VARCHAR(65)"},
                query_label=_label("encoded-exact-bound"),
                **common,
            )
            == 2
        )
    assert submitted_creates
    actual = sql.read(
        target_alias,
        f"SELECT row_id, items, nullable_marker FROM {fitting} ORDER BY row_id",
        retry_cnt=1,
        query_label=_label("encoded-check"),
    )
    assert list(actual.itertuples(index=False, name=None)) == [
        (1, _ENCODED_EMPTY_ARRAY, None),
        (2, _ENCODED_EMPTY_ARRAY, ""),
    ]
