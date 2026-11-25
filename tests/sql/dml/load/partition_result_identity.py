"""The disposable partition assertion consumes actual nullable read dataframes."""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
from analytics_toolkit.sql.dml.io.dataframes import (
    column_result_from_rows,
    dataframe_from_column_result,
)

from tests.sql.integration.core import audit_literal_values


def test_string_partition_scenario_accepts_sql_null_from_the_public_dataframe_constructor(
    monkeypatch, write_sql_connections
):
    loads = []
    reads = []

    def load(_alias, _table, frame, **_options):
        loads.append(frame.copy(deep=True))
        return len(frame)

    def read(_alias, query, **_options):
        reads.append(query)
        rows = list(loads[1].itertuples(index=False, name=None))
        rows.extend(loads[0].iloc[3:].itertuples(index=False, name=None))
        frame = dataframe_from_column_result(
            column_result_from_rows(("row_id", "partition_name", "payload"), rows)
        )
        assert frame.iloc[-1, 1] is pd.NA
        return frame

    monkeypatch.setattr(audit_literal_values.sql, "load_df", load)
    monkeypatch.setattr(audit_literal_values.sql, "read", read)
    connections = {
        "trino_target_values": {
            "type": "trino",
            "host": "fake.invalid",
            "port": 8080,
            "user": "fake",
            "catalog": "iceberg",
            "schema": "sandbox",
        }
    }
    registry = SimpleNamespace(table=lambda _alias, table: table)

    audit_literal_values.test_trino_upsert_treats_marker_shaped_partitions_as_stored_strings(
        registry, connections, write_sql_connections
    )

    assert len(loads) == 2
    assert len(reads) == 1
