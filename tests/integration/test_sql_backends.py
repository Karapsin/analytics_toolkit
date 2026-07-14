from __future__ import annotations

import os
import uuid

import pandas as pd
import pytest
from analytics_toolkit import sql

pytestmark = pytest.mark.integration


def _table_name(backend: str, suffix: str) -> str:
    token = uuid.uuid4().hex[:10]
    if backend == "gp":
        return f"public.it_{suffix}_{token}"
    if backend == "trino":
        return f"iceberg.integration.it_{suffix}_{token}"
    return f"integration.it_{suffix}_{token}"


def _enabled_backends() -> list[str]:
    backends = ["trino", "ch"]
    if os.environ.get("SQL_INTEGRATION_GP") == "1":
        backends.insert(0, "gp")
    return backends


def _load_options(backend: str) -> dict[str, object]:
    if backend == "gp":
        return {"gp_distributed_by_key": "id"}
    if backend == "trino":
        return {"partition_by": ["dt"]}
    return {
        "partition_by": ["dt"],
        "order_by": ["id"],
        "ch_cluster": "integration_cluster",
    }


def test_connections_are_valid_and_reachable() -> None:
    results = sql.validate_connections(_enabled_backends(), connect=True)

    assert [(result.connection_key, result.valid, result.connected) for result in results] == [
        (backend, True, True) for backend in _enabled_backends()
    ]


@pytest.mark.parametrize("backend", ["gp", "trino", "ch"])
def test_backend_load_metadata_upsert_and_cleanup(backend: str) -> None:
    if backend not in _enabled_backends():
        pytest.skip("Greenplum integration runs only on x86_64")
    table = _table_name(backend, "lifecycle")
    initial = pd.DataFrame(
        {
            "id": [1, 2],
            "dt": [pd.Timestamp("2026-01-01"), pd.Timestamp("2026-01-02")],
            "value": ["one", "two"],
        }
    )
    update = pd.DataFrame(
        {
            "id": [2, 3],
            "dt": [pd.Timestamp("2026-01-02"), pd.Timestamp("2026-01-03")],
            "value": ["two-updated", "three"],
        }
    )
    options = _load_options(backend)

    try:
        assert sql.load_df(backend, table, initial, write_mode="replace", **options) == 2
        assert sql.load_df(backend, table, initial.iloc[:1], write_mode="append", **options) == 1

        upsert_options = dict(options)
        if backend != "gp":
            upsert_options["upsert_partition_column"] = "dt"
        assert (
            sql.load_df(
                backend,
                table,
                update,
                write_mode="upsert",
                key_columns=["id"],
                **upsert_options,
            )
            == 2
        )

        frame = sql.read(backend, f"SELECT id, value FROM {table} ORDER BY id, value")
        assert set(frame["id"].tolist()) == {1, 2, 3}
        assert "two-updated" in frame["value"].tolist()

        listed = sql.show_tables(backend, table_name=table)
        assert table.rsplit(".", 1)[-1] in listed["table_name"].tolist()
        assert "CREATE" in sql.extract_ddl(backend, table).upper()
    finally:
        sql.drop_tables(
            backend,
            table,
            if_exists=True,
            ch_cluster="integration_cluster" if backend == "ch" else None,
        )


def test_representative_cross_backend_transfers() -> None:
    if os.environ.get("SQL_INTEGRATION_GP") != "1":
        pytest.skip("cross-backend chain includes Greenplum and runs on x86_64")
    gp_table = _table_name("gp", "transfer_source")
    trino_table = _table_name("trino", "transfer_mid")
    ch_table = _table_name("ch", "transfer_mid")
    gp_roundtrip = _table_name("gp", "transfer_roundtrip")
    source = pd.DataFrame(
        {
            "id": [10, 20, 30],
            "dt": [pd.Timestamp("2026-02-01")] * 3,
            "value": ["ten", "twenty", "thirty"],
        }
    )

    try:
        sql.load_df("gp", gp_table, source, write_mode="replace", gp_distributed_by_key="id")
        assert sql.transfer(
            "gp",
            "trino",
            from_table=gp_table,
            to_table=trino_table,
            write_mode="replace",
            batch_size=2,
            adaptive_batch_size=False,
            target_rows_per_second=False,
            partition_by=["dt"],
        ) == len(source)
        assert sql.transfer(
            "trino",
            "ch",
            from_table=trino_table,
            to_table=ch_table,
            write_mode="replace",
            batch_size=2,
            adaptive_batch_size=False,
            target_rows_per_second=False,
            partition_by=["dt"],
            order_by=["id"],
            ch_cluster="integration_cluster",
        ) == len(source)
        assert sql.transfer(
            "ch",
            "gp",
            from_table=ch_table,
            to_table=gp_roundtrip,
            write_mode="replace",
            batch_size=2,
            adaptive_batch_size=False,
            target_rows_per_second=False,
            gp_distributed_by_key="id",
        ) == len(source)
        roundtrip = sql.read("gp", f"SELECT id, value FROM {gp_roundtrip} ORDER BY id")
        assert roundtrip.to_dict("records") == source[["id", "value"]].to_dict("records")
    finally:
        sql.drop_tables("gp", [gp_table, gp_roundtrip], if_exists=True, ch_cluster=None)
        sql.drop_tables("trino", trino_table, if_exists=True, ch_cluster=None)
        sql.drop_tables(
            "ch",
            ch_table,
            if_exists=True,
            ch_cluster="integration_cluster",
        )
