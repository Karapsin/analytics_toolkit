from __future__ import annotations

from tests.sql._support.row_batches import (
    importlib,
    inspect,
    make_gp_config,
    pytest,
    transfer_api_module,
)


def test_transfer_public_signature_exposes_concurrency_caps() -> None:
    sql_module = importlib.import_module("analytics_toolkit.sql")
    signature = inspect.signature(sql_module.transfer)

    assert signature.parameters["soft_concurrency_cap"].default is None
    assert signature.parameters["hard_concurrency_cap"].default == 5
    assert list(signature.parameters)[-2:] == [
        "soft_concurrency_cap",
        "hard_concurrency_cap",
    ]


def test_transfer_split_concurrency_supports_keyed_source_staging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configs = {
        "source": make_gp_config("source", transfer_staging_schema="source_stage"),
        "target": make_gp_config("target", transfer_staging_schema="target_stage"),
    }
    monkeypatch.setattr(
        transfer_api_module,
        "get_connection_config",
        lambda db_key: configs[db_key],
    )

    plan = transfer_api_module.transfer_table(
        from_db="source",
        to_db="target",
        from_sql="select id from source_table where {event_date}",
        to_table="sandbox.target",
        table_schema={"id": "INTEGER"},
        transfer_keys="event_date",
        transfer_key_values=[1, 2, 3],
        read_concurrency=6,
        write_concurrency=2,
        target_batch_memory_mb=4,
        hard_concurrency_cap=6,
        dry_run=True,
    )

    assert plan.options["source_staging_mode"] == "source_staged"
    assert plan.options["source_stage_count"] == 3
    assert plan.options["live_source_stage_limit"] == 5
    assert plan.options["source_stage_phase_barrier"] is False
    assert plan.options["source_stage_creation"] == "lazy_per_key"
    assert plan.options["writer_scheduling"] == "whole_key"
    assert plan.options["queue_capacity"] == 2
    assert plan.options["batch_queue_capacity_per_writer"] == 1
    assert plan.options["resident_batch_slots"] == 4
    assert plan.options["target_batch_memory_scope"] == "aggregate_resident_batches"
    assert plan.options["target_memory_bytes_per_resident_batch"] == 1024 * 1024
    assert plan.options["reader_scheduling"] == "dynamic_pending_key_claims"
    assert plan.options["target_stage_count_is_maximum"] is True
    assert plan.options["target_stage_creation"] == "lazy_first_non_empty_key"
    assert plan.options["target_stage_maximum"] == 2
    assert plan.options["reader_slice_assignments"] is None
    assert plan.options["source_stage_lifecycle"] == (
        "per_key_ctas_count_stream_validate_acknowledge_drop"
    )
    phases = [step.phase for step in plan.statements]
    assert phases.count("create_stage_if_needed") == 2
    assert phases.count("create_stage") == 0
    assert phases.count("materialize_source_stage") == 3
    assert phases.count("count_source_stage") == 3
    assert phases.count("load_stage") == 3
    assert phases.count("validate_key_stage") == 3
    assert phases.count("drop_source_stage") == 3
    assert phases.count("drop_stage_if_created") == 2
    lifecycle_phases = [
        phase
        for phase in phases
        if phase
        in {
            "materialize_source_stage",
            "count_source_stage",
            "load_stage",
            "validate_key_stage",
            "drop_source_stage",
        }
    ]
    assert lifecycle_phases == [
        phase
        for _slice in range(3)
        for phase in (
            "materialize_source_stage",
            "count_source_stage",
            "load_stage",
            "validate_key_stage",
            "drop_source_stage",
        )
    ]
    assert not any("worker 0 streamed keyed source slice batches" in sql for sql in plan.sqls)
    assert any("dynamically scheduled ready whole-key batches" in sql for sql in plan.sqls)
    assert plan.metadata.ignore_source_staging is False
    assert plan.metadata.source_staging_mode == "source_staged"
    assert plan.metadata.source_stage_count == 3
    assert plan.metadata.live_source_stage_limit == 5
