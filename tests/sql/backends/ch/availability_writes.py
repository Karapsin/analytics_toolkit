"""Optional facade outages survive public creation, population and stage cleanup."""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.created_resources import CreatedIdentity
from analytics_toolkit.sql.backends.transfer_stage import is_transfer_stage_identifier

from tests.sql._support.availability_writes import (
    configure_optional_facade,
    observe_created_resources,
)


@pytest.mark.parametrize("transport", ["http", "native"])
@pytest.mark.parametrize("partial", [False, True], ids=["all-facades-down", "partial-facade"])
@pytest.mark.parametrize("operation", ["load", "query", "transfer", "keyed_transfer"])
def test_public_writes_finish_with_only_acknowledged_optional_facade_resources(
    outage: Any, monkeypatch: Any, transport: str, partial: bool, operation: str
) -> None:
    cluster = configure_optional_facade(outage, transport, partial=partial)
    captured, dropped = observe_created_resources(monkeypatch)
    table = f"db.optional_{operation}"
    with pytest.warns(sql.ClickHouseDegradedClusterWarning, match="not required.*storage"):  # noqa: PT031 -- same acceptance checks for each public workflow.
        if operation == "load":
            result = sql.load_df(
                "target",
                table,
                pd.DataFrame({"id": [17, 19]}),
                table_schema={"id": "UInt64"},
                write_mode="append",
                retry_cnt=1,
                timeout_increment=0,
                return_metadata=True,
            )
            expected = [17, 19]
        elif operation == "query":
            result = sql.create_table(
                "target",
                table,
                sql="SELECT id FROM db.events",
                insert_data=True,
                retry_cnt=1,
                timeout_increment=0,
                return_metadata=True,
            )
            expected = [1, 2]
        else:
            keyed = operation == "keyed_transfer"
            result = sql.transfer(
                from_db="source",
                to_db="target",
                from_sql="SELECT id FROM db.events WHERE {id}"
                if keyed
                else "SELECT id FROM db.events",
                to_table=table,
                table_schema={"id": "UInt64"},
                key_columns="id",
                transfer_keys=["id"] if keyed else None,
                transfer_key_values=[1, 2] if keyed else None,
                concurrency=1,
                write_mode="append",
                ignore_source_staging=True,
                retry_cnt=1,
                full_retry_cnt=1,
                timeout_increment=0,
                full_timeout_increment=0,
                return_metadata=True,
            )
            expected = [1, 2]
    assert result.metadata.data_complete is True
    values = sql.read("target", f"SELECT id FROM {table}", retry_cnt=1).id.tolist()
    assert sorted(values) == expected
    assert captured
    assert all(isinstance(resource.certificate, CreatedIdentity) for resource in captured)
    assert all(resource.scope.cluster != "edge" for resource in captured)
    assert not any(host in {"h3", "h4"} for host, _query, _settings in cluster.commands)
    assert not any("ON CLUSTER edge" in query for _host, query, _settings in cluster.commands)
    if operation in {"transfer", "keyed_transfer"}:
        assert dropped
    assert not any(
        is_transfer_stage_identifier(name.split(".", 1)[1]) for _host, name in cluster.tables
    )
