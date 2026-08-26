from __future__ import annotations

from tests.sql._support.staged_keyed import (
    SimpleNamespace,
    SqlPlan,
    _options,
    build_ch_shard_table_name,
    dry_run,
)


def test_lazy_clickhouse_dry_run_cleans_every_conditional_stage_companion() -> None:
    policy = SimpleNamespace(
        create_distributed_pair=True,
        shard_on_cluster="STAGE_SHARDS",
        distributed_on_cluster="STAGE_DISTRIBUTED",
    )
    options = _options(to_db_backend="ch", staging_ch_policy=policy)
    plan = SqlPlan(operation="transfer_table")

    dry_run._add_target_stage_cleanup(
        plan,
        options,
        "scratch.transfer_stage",
        lazy=True,
    )

    shard_table = build_ch_shard_table_name("scratch.transfer_stage")
    assert len(plan.statements) == 4
    assert all(step.phase == "drop_stage_if_created" for step in plan.statements)
    assert any(shard_table in step.sql for step in plan.statements)
    assert any("STAGE_SHARDS" in step.sql for step in plan.statements)
    assert any("STAGE_DISTRIBUTED" in step.sql for step in plan.statements)
