from __future__ import annotations

from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from sqlglot import exp, parse_one
from sqlparse import parse

from tests.sql._support.gp_upsert_load import native_upsert_load
from tests.sql._support.load_table import (
    TEST_CH_SHARD_TABLE,
    TEST_CH_STAGE_TABLE,
    TEST_CH_TABLE,
    FakeClickHouseClient,
    load_df_module,
    pd,
    table_ops_module,
)
from tests.sql._support.transfer_schema import stage_module


def test_finalize_stage_table_clickhouse_upsert_drops_shard_partitions() -> None:
    client = FakeClickHouseClient()
    batch = pd.DataFrame({"id": [1], "score": [10]})

    table_ops_module.finalize_stage_table(
        connection_type="ch",
        connection=client,
        stage_table=TEST_CH_STAGE_TABLE,
        target_table=TEST_CH_TABLE,
        replace_target_table=True,
        target_exists=True,
        sample_batch=batch,
        write_mode="upsert",
        key_columns=["id"],
        upsert_partition_column="id",
        final_upsert_stage_table=f"{TEST_CH_TABLE}__final_stage",
        insert_column_types={"id": "UInt64", "score": "Int64"},
        ch_cluster="core",
    )

    drop_sql = next(sql for sql in client.commands if "DROP PARTITION" in sql)
    tree = parse_one(drop_sql, read=ClickHouseDialect)
    assert tree.this.db == "default"
    assert tree.this.name == TEST_CH_SHARD_TABLE
    assert tree.args["cluster"].this.name == "core"
    assert "DROP PARTITION 1" in drop_sql
    assert client.commands[-1].startswith(f"INSERT INTO {TEST_CH_TABLE} (`id`, `score`) ")


def test_load_df_drops_overlap_stage_table_after_success(monkeypatch) -> None:
    database = native_upsert_load(monkeypatch, {"id": "BIGINT", "value": "INTEGER"})
    original_rows = [dict(row) for row in database.target.rows]
    expected_stage = "public.target__stage__ok"
    monkeypatch.setattr(stage_module, "build_stage_table_name", lambda *_a, **_k: expected_stage)

    inserted_rows = load_df_module.load_df(
        "gp",
        "public.target",
        pd.DataFrame({"id": [1, 2], "value": [10, 20]}),
        append=True,
        key_columns=["id"],
        progress=False,
        retry_cnt=1,
        timeout_increment=0,
    )

    assert inserted_rows == 2
    assert database.target.rows == [*original_rows, {"id": 1, "value": 10}, {"id": 2, "value": 20}]
    drops = [
        (table.db, table.name)
        for command in database.commands
        if parse(command)[0].get_type() == "DROP"
        if isinstance((tree := parse_one(command, read="postgres")), exp.Drop)
        for table in tree.find_all(exp.Table)
    ]
    assert drops == [("public", expected_stage.split(".", 1)[1])]
    assert set(database.tables) == {("public", "target")}
