from __future__ import annotations

from tests.sql._support.policies import (
    trino_adapter_module,
)


def test_trino_upsert_placeholder_builds_complete_stage_sqls() -> None:
    statements = trino_adapter_module.TrinoAdapter().build_upsert_stage_placeholder_sqls(
        "iceberg.stage.target",
        "iceberg.stage.incoming",
        key_columns=["id"],
        upsert_partition_column="dt",
        final_stage_table="iceberg.stage.final",
        partition_values=["2026-01-01"],
        trino_partition_drop_sql_template=(
            "ALTER TABLE {table} DROP PARTITION ({partition_column} = {partition_value})"
        ),
    )

    assert len(statements) >= 2
