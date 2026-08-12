from __future__ import annotations

# ruff: noqa: EM101, I001, PT011, PT018, TRY003

import threading
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

from analytics_toolkit.sql.backends.models import SourceColumn
from analytics_toolkit.sql.backends import transfer_stage
from analytics_toolkit.sql.backends import source_count
from analytics_toolkit.sql.dml.load import stage as load_stage
from analytics_toolkit.sql.dml.transfer.flow import (
    api,
    attempt,
    dry_run,
    parquet_batches,
    parquet_stage,
    staged_attempt,
    staged_keyed_pipeline,
    superseded,
)
from analytics_toolkit.sql.dml.transfer.flow.range_scheduler import (
    AdaptiveRangeScheduler,
    OrdinalRange,
)
from analytics_toolkit.sql.dml.transfer.flow.source_snapshot import (
    build_snapshot_range_sql,
    build_snapshot_select_sql,
    build_source_snapshot_sql,
)
from analytics_toolkit.sql.dml.transfer.flow.stage_identity import (
    assert_transfer_identity,
    resolve_destination_identity,
    resolve_internal_columns,
)
from analytics_toolkit.sql.dml.transfer.flow import stage_validation
from analytics_toolkit.sql.dml.transfer.flow.superseded import (
    cleanup_superseded_transfer_stages,
)
from analytics_toolkit.sql.dml.transfer.runtime.models import (
    RowBatch,
    TransferOptions,
    TransferConcurrency,
    TransferSlice,
    TransferStageState,
)
from analytics_toolkit.sql.dml.transfer import schema as transfer_schema


def _staged_options(**overrides: Any) -> TransferOptions:
    values: dict[str, Any] = {
        "from_db_key": "source",
        "from_db_backend": "gp",
        "to_db_key": "target",
        "to_db_backend": "gp",
        "source_sql": "SELECT id FROM source",
        "target_table": "public.target",
        "transfer_id": "a" * 32,
        "canonical_destination_identity": "public.target",
        "destination_hash": "0123456789abcdef",
        "source_transfer_staging_schema": "source_stage",
        "transfer_staging_schema": "target_stage",
        "batch_size": 2,
        "min_batch_size": 1,
        "max_batch_size": 4,
        "adaptive_batch_size": False,
        "full_retry_cnt": 2,
    }
    values.update(overrides)
    return TransferOptions(**values)


def test_destination_identity_preserves_quoting_and_normalizes_unquoted() -> None:
    unquoted = resolve_destination_identity("Sales.Orders", "gp")
    quoted = resolve_destination_identity('"Sales"."Orders"', "gp")

    assert unquoted.canonical == "sales.orders"
    assert quoted.canonical == '"Sales"."Orders"'
    assert unquoted.hash_prefix == unquoted.fingerprint[:16]
    assert quoted.hash_prefix != unquoted.hash_prefix


def test_internal_columns_resolve_case_and_suffix_collisions() -> None:
    columns = resolve_internal_columns(
        [
            "__ANALYTICS_TOOLKIT_TRANSFER_ID",
            "__analytics_toolkit_destination_table",
            "__analytics_toolkit_row_ordinal",
            "__analytics_toolkit_row_ordinal_1",
        ],
        "gp",
    )

    assert columns.transfer_id == "__analytics_toolkit_transfer_id_1"
    assert columns.destination_table == "__analytics_toolkit_destination_table_1"
    assert columns.row_ordinal == "__analytics_toolkit_row_ordinal_2"
    assert len(set(columns.names())) == 4


def test_hashed_stage_name_keeps_prefix_and_gp_byte_limit() -> None:
    identity = resolve_destination_identity("sales.orders", "gp")
    name = load_stage.build_stage_table_name(
        "gp",
        "sales.orders",
        transfer_staging_schema="staging",
        random_suffix="a" * 32,
        destination_hash=identity.hash_prefix,
    )
    relation = name.split(".")[-1].strip('"')

    assert relation.startswith(f"{identity.hash_prefix}__")
    assert "a" * 32 in relation
    assert len(relation.encode()) <= 63


@pytest.mark.parametrize(
    ("target_table", "username", "stage_suffix"),
    [
        ("sales.orders", None, "abcd1234"),
        ("sales.orders", "integration_user", "a" * 32 + "__w00000"),
        ("sales." + "😀" * 40, "integration_user", "b" * 32 + "__source"),
    ],
)
def test_stage_identifiers_use_exact_gp_style_on_every_backend(
    target_table: str,
    username: str | None,
    stage_suffix: str,
) -> None:
    names = {
        backend: load_stage.build_stage_table_name(
            backend,
            target_table,
            transfer_staging_schema="staging",
            transfer_staging_username=username,
            random_suffix=stage_suffix,
            destination_hash="0123456789abcdef",
        )
        for backend in ("gp", "trino", "ch")
    }
    identifiers = {backend: name.split(".")[-1].strip('"`') for backend, name in names.items()}

    assert identifiers["trino"] == identifiers["gp"]
    assert identifiers["ch"] == identifiers["gp"]
    assert identifiers["gp"].startswith("0123456789abcdef__")
    assert identifiers["gp"].endswith(stage_suffix)
    assert len(identifiers["gp"].encode()) <= 63


def test_legacy_stage_identifiers_use_gp_fitting_on_every_backend() -> None:
    names = {
        backend: load_stage.build_stage_table_name(
            backend,
            "sales." + "long_destination_name_" * 8,
            transfer_staging_schema="staging",
            transfer_staging_username="integration_user",
            random_suffix="abcd1234",
        )
        for backend in ("gp", "trino", "ch")
    }
    identifiers = {backend: name.split(".")[-1].strip('"`') for backend, name in names.items()}

    assert identifiers["trino"] == identifiers["gp"]
    assert identifiers["ch"] == identifiers["gp"]
    assert len(identifiers["gp"].encode()) <= 63


def test_explicit_stage_suffix_collision_allocates_new_name(monkeypatch: Any) -> None:
    existence_checks = 0
    created: list[str] = []

    def fake_exists(*_args: Any, **_kwargs: Any) -> bool:
        nonlocal existence_checks
        existence_checks += 1
        return existence_checks == 1

    monkeypatch.setattr(load_stage, "table_exists", fake_exists)
    monkeypatch.setattr(
        load_stage,
        "_create_sql_table_with_connection",
        lambda _backend, _connection, table, *_args, **_kwargs: created.append(table),
    )
    actual = load_stage.create_stage_table(
        "trino",
        object(),
        "sales.orders",
        pd.DataFrame({"id": [1]}),
        random_suffix="transferid__w00000",
        destination_hash="0123456789abcdef",
    )

    assert actual == created[0]
    relation = actual.split(".")[-1].strip('"')
    assert relation.startswith("0123456789abcdef__orders")
    assert relation[:-5].endswith("transferid__w00000")
    assert len(relation[-5:]) == 5
    assert len(relation.encode()) <= 63


def test_stage_creation_race_reallocates_and_hashed_prefix_is_stable(monkeypatch: Any) -> None:
    existence = iter([False, True, False])
    creates = 0

    def create(*_args: Any, **_kwargs: Any) -> None:
        nonlocal creates
        creates += 1
        if creates == 1:
            raise RuntimeError("duplicate table")

    monkeypatch.setattr(load_stage, "table_exists", lambda *_args, **_kwargs: next(existence))
    monkeypatch.setattr(load_stage, "_create_sql_table_with_connection", create)
    actual = load_stage.create_stage_table(
        "trino",
        object(),
        "sales.orders",
        pd.DataFrame({"id": [1]}),
        random_suffix="transferid__w00000",
        destination_hash="0123456789abcdef",
    )
    relation = actual.split(".")[-1].strip('"')
    assert relation[:-5].endswith("transferid__w00000")
    assert len(relation.encode()) <= 63
    assert load_stage.build_stage_table_prefix(
        "trino", "sales.orders", None, "0123456789abcdef"
    ) == load_stage.build_stage_table_prefix("gp", "sales.orders", None, "0123456789abcdef")

    monkeypatch.setattr(load_stage, "table_exists", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        load_stage,
        "_create_sql_table_with_connection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("create failed")),
    )
    with pytest.raises(RuntimeError, match="create failed"):
        load_stage.create_stage_table(
            "trino",
            object(),
            "sales.orders",
            pd.DataFrame({"id": [1]}),
            destination_hash="0123456789abcdef",
        )


def test_adaptive_ranges_cover_slices_once_after_split_retry() -> None:
    scheduler = AdaptiveRangeScheduler({0: 5, 1: 2, 2: 0})
    failed = scheduler.claim(0, 5)
    assert failed == OrdinalRange(0, 1, 6)
    scheduler.requeue_failed(0, failed, reduced_batch_size=2)

    while True:
        claimed = scheduler.claim(1, 10)
        if claimed is None:
            break
        scheduler.complete(1, claimed)

    scheduler.validate_complete()
    assert sum(item.row_count for item in scheduler.completed_ranges()) == 7


def test_snapshot_sql_keeps_paging_metadata_source_local() -> None:
    columns = resolve_internal_columns(["id"], "gp")
    select_sql = build_snapshot_select_sql(
        backend="gp",
        source_sql="SELECT id FROM source_table;",
        source_columns=["id"],
        transfer_id="a" * 32,
        canonical_destination="sales.orders",
        slice_id=4,
        internal_columns=columns,
    )
    snapshot = build_source_snapshot_sql(
        backend="gp",
        snapshot_table="staging.snapshot",
        snapshot_select_sql=select_sql,
        internal_columns=columns,
    )
    range_sql = build_snapshot_range_sql(
        backend="gp",
        snapshot_table="staging.snapshot",
        source_columns=["id"],
        internal_columns=columns,
        transfer_id="a" * 32,
        canonical_destination="sales.orders",
        ordinal_range=OrdinalRange(4, 10, 20),
    )

    assert "row_number() OVER (PARTITION BY 4)" in select_sql
    assert "DISTRIBUTED RANDOMLY" in snapshot.create_sql
    assert snapshot.post_create_sqls[0].startswith("CREATE INDEX")
    assert "__analytics_toolkit_transfer_id" not in select_sql
    assert "__analytics_toolkit_destination_table" not in select_sql
    assert "__analytics_toolkit_transfer_id" not in range_sql
    assert "__analytics_toolkit_destination_table" not in range_sql
    assert range_sql.startswith('SELECT "id" FROM staging.snapshot')
    assert ">= 10" in range_sql and "< 20" in range_sql
    assert range_sql.endswith('ORDER BY "__analytics_toolkit_row_ordinal" LIMIT 10')


def test_public_transfer_reuses_one_runtime_id_and_returns_it(monkeypatch: Any) -> None:
    template = TransferOptions(
        from_db_key="source",
        from_db_backend="gp",
        to_db_key="target",
        to_db_backend="gp",
        source_sql="SELECT id FROM source",
        target_table="public.target",
        replace_target_table=False,
        canonical_destination_identity="public.target",
        destination_hash="0123456789abcdef",
    )
    seen: list[str | None] = []
    monkeypatch.setattr(api, "build_transfer_options", lambda **_kwargs: template)
    monkeypatch.setattr(
        api,
        "run_transfer_attempt",
        lambda options, **_kwargs: seen.append(options.transfer_id) or 3,
    )
    monkeypatch.setattr(
        api,
        "best_effort_transfer_target_count",
        lambda _options, **_kwargs: 3,
    )

    first = api.transfer_table("source", "target", return_metadata=True)
    second = api.transfer_table("source", "target", return_metadata=True)

    assert first.metadata.transfer_id == seen[0]
    assert second.metadata.transfer_id == seen[1]
    assert len(first.metadata.transfer_id or "") == 32
    assert first.metadata.transfer_id != second.metadata.transfer_id


def test_transfer_metadata_uses_range_reduced_unkeyed_concurrency(monkeypatch: Any) -> None:
    template = _staged_options(
        transfer_concurrency=TransferConcurrency(
            legacy_value=None,
            requested_read=8,
            requested_write=4,
            effective_read=2,
            effective_write=2,
            split_requested=True,
            soft_concurrency_cap=2,
            hard_concurrency_cap=5,
            soft_limited_read=2,
            soft_limited_write=2,
        )
    )
    monkeypatch.setattr(api, "build_transfer_options", lambda **_kwargs: template)

    def transfer_attempt(options: TransferOptions, **_kwargs: Any) -> int:
        concurrency = options.transfer_concurrency
        object.__setattr__(
            options,
            "transfer_concurrency",
            TransferConcurrency(
                legacy_value=concurrency.legacy_value,
                requested_read=concurrency.requested_read,
                requested_write=concurrency.requested_write,
                effective_read=1,
                effective_write=1,
                split_requested=concurrency.split_requested,
                soft_concurrency_cap=concurrency.soft_concurrency_cap,
                hard_concurrency_cap=concurrency.hard_concurrency_cap,
                soft_limited_read=concurrency.soft_limited_read,
                soft_limited_write=concurrency.soft_limited_write,
            ),
        )
        return 1

    monkeypatch.setattr(api, "run_transfer_attempt", transfer_attempt)
    monkeypatch.setattr(api, "best_effort_transfer_target_count", lambda *_args, **_kwargs: 1)

    result = api.transfer_table("source", "target", return_metadata=True)

    assert result.metadata.requested_read_concurrency == 8
    assert result.metadata.requested_write_concurrency == 4
    assert result.metadata.soft_limited_read_concurrency == 2
    assert result.metadata.soft_limited_write_concurrency == 2
    assert result.metadata.effective_read_concurrency == 1
    assert result.metadata.effective_write_concurrency == 1


def test_lazy_keyed_metadata_reuses_bounded_final_target_count(monkeypatch: Any) -> None:
    build_calls: list[bool] = []
    transfer_slice = TransferSlice(
        index=0,
        values=(1,),
        predicate_sql="id = 1",
        source_sql="SELECT 1 AS id",
        label="id:1",
    )

    def build_options(**kwargs: Any) -> TransferOptions:
        collect_count = bool(kwargs["collect_final_target_count"])
        build_calls.append(collect_count)
        return _staged_options(
            transfer_keys=["id"],
            transfer_slices=[transfer_slice],
            collect_final_target_count=collect_count,
        )

    def transfer_attempt(options: TransferOptions, **_kwargs: Any) -> int:
        assert options.collect_final_target_count is True
        object.__setattr__(options, "final_target_rows", 17)
        return 1

    monkeypatch.setattr(api, "build_transfer_options", build_options)
    monkeypatch.setattr(api, "run_transfer_attempt", transfer_attempt)
    monkeypatch.setattr(
        api,
        "best_effort_transfer_target_count",
        lambda *_args, **_kwargs: pytest.fail("opened an unbudgeted metadata-count connection"),
    )

    result = api.transfer_table("source", "target", return_metadata=True)

    assert build_calls == [True]
    assert result.metadata.final_target_rows == 17


def test_transfer_does_not_full_retry_nonretryable_post_finalization_close(
    monkeypatch: Any,
) -> None:
    class FinalizedCloseError(RuntimeError):
        analytics_toolkit_sql_retry_safe = False

    options = _staged_options(
        transfer_slices=[TransferSlice(0, (1,), "", "SELECT 1 AS id", "key=1")],
        transfer_keys=["key"],
        full_retry_cnt=5,
        full_timeout_increment=0,
    )
    attempts: list[int] = []
    error = FinalizedCloseError("target connection remains live")
    monkeypatch.setattr(api, "build_transfer_options", lambda **_kwargs: options)

    def fail_attempt(**_kwargs: Any) -> int:
        attempts.append(1)
        raise error

    monkeypatch.setattr(api, "run_transfer_attempt", fail_attempt)

    with pytest.raises(FinalizedCloseError) as exc_info:
        api.transfer_table("source", "target")

    assert exc_info.value is error
    assert attempts == [1]


def test_superseded_cleanup_uses_reserved_stage_names(monkeypatch: Any) -> None:
    dropped: list[str] = []
    adapter = SimpleNamespace(
        query_transfer_stage_table_names=lambda *_args, **_kwargs: [
            "0123456789abcdef__orders" + "a" * 32 + "__w00000",
            "0123456789abcdef__orders" + "b" * 32 + "__source",
            "0123456789abcdef__orders" + "d" * 32 + "__other",
        ],
        qualify_transfer_stage_table_name=lambda _key, schema, table: f"{schema}.{table}",
    )
    monkeypatch.setattr(
        "analytics_toolkit.sql.dml.transfer.flow.superseded.get_backend_adapter",
        lambda _backend: adapter,
    )
    monkeypatch.setattr(
        "analytics_toolkit.sql.dml.transfer.flow.superseded.cleanup_stage_table",
        lambda _backend, _connection, table, **_kwargs: dropped.append(table),
    )
    options = TransferOptions(
        from_db_key="source",
        from_db_backend="gp",
        to_db_key="target",
        to_db_backend="gp",
        source_sql="SELECT 1",
        target_table="sales.orders",
        transfer_id="c" * 32,
        canonical_destination_identity="sales.orders",
        destination_hash="0123456789abcdef",
    )

    cleanup_superseded_transfer_stages(
        options=options,
        connection=object(),
        backend="gp",
        connection_key="target",
        staging_schema="staging",
        internal_columns=resolve_internal_columns(["id"], "gp"),
    )

    assert dropped == [
        "staging.0123456789abcdef__orders" + "a" * 32 + "__w00000",
        "staging.0123456789abcdef__orders" + "b" * 32 + "__source",
    ]


def test_staged_attempt_orchestrates_snapshot_ranges_and_finalization(
    monkeypatch: Any,
) -> None:
    options = _staged_options(
        concurrency=8,
        transfer_concurrency=TransferConcurrency(
            legacy_value=8,
            requested_read=8,
            requested_write=8,
            effective_read=2,
            effective_write=2,
            split_requested=False,
            soft_concurrency_cap=2,
            hard_concurrency_cap=5,
            soft_limited_read=2,
            soft_limited_write=2,
        ),
    )
    state = TransferStageState(target_exists=True)
    finalized: list[tuple[int, list[str]]] = []
    worker_counts: list[int] = []

    class Scheduler:
        def validate_complete(self) -> None:
            finalized.append((-1, []))

    monkeypatch.setattr(staged_attempt, "get_sql_connection", lambda _key: object())
    monkeypatch.setattr(staged_attempt, "create_stage_state", lambda *_args: state)
    monkeypatch.setattr(
        staged_attempt,
        "inspect_source_query_schema",
        lambda *_args: [SourceColumn("id", "bigint")],
    )
    monkeypatch.setattr(
        staged_attempt, "cleanup_superseded_transfer_stages", lambda **_kwargs: None
    )
    monkeypatch.setattr(
        staged_attempt,
        "map_source_schema_to_target",
        lambda *_args, **_kwargs: {"id": "BIGINT"},
    )
    monkeypatch.setattr(staged_attempt, "ensure_transfer_target_table", lambda *_args: None)
    monkeypatch.setattr(staged_attempt, "_materialize_snapshot", lambda *_args: ("snap", {0: 6}))

    refreshed_target = object()
    refreshes: list[str] = []

    def create_worker_stages(
        _options: TransferOptions,
        target_ref: dict[str, Any],
        _state: TransferStageState,
        **_kwargs: Any,
    ) -> list[str]:
        assert target_ref["connection"] is refreshed_target
        worker_counts.append(_kwargs["worker_count"])
        return ["worker_stage"]

    monkeypatch.setattr(
        staged_attempt,
        "_create_worker_stages",
        create_worker_stages,
    )
    monkeypatch.setattr(staged_attempt, "AdaptiveRangeScheduler", lambda _counts: Scheduler())

    def run_range_workers(*_args: Any, **kwargs: Any) -> None:
        progress = kwargs["transfer_progress"]
        completed_at = progress.now()
        progress.commit_batch(
            logical_batch_id=(0, 1, 7),
            worker_id=0,
            batch=RowBatch(["id"], [(value,) for value in range(6)]),
            read_started_at=completed_at,
            read_completed_at=completed_at,
            insert_completed_at=completed_at,
        )

    monkeypatch.setattr(staged_attempt, "_run_range_workers", run_range_workers)
    monkeypatch.setattr(staged_attempt, "_consolidate_worker_stages", lambda *_args: None)
    monkeypatch.setattr(staged_attempt, "validate_loaded_stage_row_count", lambda **_kwargs: None)
    monkeypatch.setattr(
        staged_attempt,
        "finalize_loaded_stage",
        lambda _options, _refs, stage_state, total: finalized.append(
            (total, list(stage_state.stage_tables or []))
        ),
    )
    monkeypatch.setattr(staged_attempt, "cleanup_stage", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(staged_attempt, "cleanup_stage_table", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(staged_attempt, "close_connection_ref", lambda *_args: None)
    monkeypatch.setattr(
        staged_attempt,
        "replace_connection",
        lambda key, ref: (refreshes.append(key), ref.update(connection=refreshed_target)),
    )

    assert staged_attempt.run_staged_source_transfer_attempt(options, insert_retry_cnt=1) == 6
    assert finalized == [(-1, []), (6, [])]
    assert state.slice_counts[0].expected_rows == 6
    assert worker_counts == [2]
    assert refreshes == ["target"]

    monkeypatch.setattr(
        staged_attempt,
        "cleanup_stage_table",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("cleanup failed")),
    )
    with pytest.raises(OSError, match="cleanup failed"):
        staged_attempt.run_staged_source_transfer_attempt(options, insert_retry_cnt=1)

    monkeypatch.setattr(
        staged_attempt,
        "finalize_loaded_stage",
        lambda *_args: (_ for _ in ()).throw(ValueError("finalization failed")),
    )
    with pytest.raises(ValueError, match="finalization failed") as caught:
        staged_attempt.run_staged_source_transfer_attempt(options, insert_retry_cnt=1)
    summary = caught.value.analytics_toolkit_transfer_attempt_summary
    assert summary["phase"] == "destination finalization"
    assert summary["committed_rows"] == 6
    assert summary["elapsed_seconds"] >= 0


def test_staged_attempt_rejects_missing_identity_and_empty_schema(monkeypatch: Any) -> None:
    with pytest.raises(RuntimeError, match="runtime identity"):
        staged_attempt.run_staged_source_transfer_attempt(
            _staged_options(transfer_id=None),
            insert_retry_cnt=1,
        )

    monkeypatch.setattr(staged_attempt, "get_sql_connection", lambda _key: object())
    monkeypatch.setattr(
        staged_attempt,
        "create_stage_state",
        lambda *_args: TransferStageState(target_exists=True),
    )
    monkeypatch.setattr(staged_attempt, "inspect_source_query_schema", lambda *_args: [])
    monkeypatch.setattr(staged_attempt, "cleanup_stage", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(staged_attempt, "close_connection_ref", lambda *_args: None)

    with pytest.raises(ValueError, match="inspectable source schema"):
        staged_attempt.run_staged_source_transfer_attempt(_staged_options(), insert_retry_cnt=1)


def test_unkeyed_target_open_failure_closes_partial_source_lease(monkeypatch: Any) -> None:
    active = {"source": 0, "target": 0}
    high_water = {"source": 0, "target": 0}
    closed: list[str] = []

    class Connection:
        def __init__(self, key: str) -> None:
            self.key = key

        def close(self) -> None:
            active[self.key] -= 1
            closed.append(self.key)

    def open_connection(key: str) -> Connection:
        if key == "target":
            raise OSError("target open failed")
        active[key] += 1
        high_water[key] = max(high_water[key], active[key])
        return Connection(key)

    monkeypatch.setattr(staged_attempt, "get_sql_connection", open_connection)
    monkeypatch.setattr(
        staged_attempt,
        "create_stage_state",
        lambda *_args: pytest.fail("state creation must not run after target open failure"),
    )

    with pytest.raises(OSError, match="target open failed"):
        staged_attempt.run_staged_source_transfer_attempt(
            _staged_options(),
            insert_retry_cnt=1,
        )

    assert active == {"source": 0, "target": 0}
    assert high_water == {"source": 1, "target": 0}
    assert closed == ["source"]

    with pytest.raises(OSError, match="target open failed"):
        staged_attempt._range_worker(
            _staged_options(),
            "source.snapshot",
            ["id"],
            TransferStageState(target_exists=True),
            "target.stage",
            SimpleNamespace(),
            0,
            1,
        )
    assert active == {"source": 0, "target": 0}
    assert high_water == {"source": 1, "target": 0}
    assert closed == ["source", "source"]


def test_unkeyed_base_exception_cleanup_does_not_mask_original(monkeypatch: Any) -> None:
    connections: list[Any] = []
    cleanup_calls: list[bool] = []

    class Connection:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    def open_connection(_key: str) -> Connection:
        connection = Connection()
        connections.append(connection)
        return connection

    monkeypatch.setattr(staged_attempt, "get_sql_connection", open_connection)
    monkeypatch.setattr(
        staged_attempt,
        "create_stage_state",
        lambda *_args: TransferStageState(target_exists=True),
    )
    monkeypatch.setattr(
        staged_attempt,
        "inspect_source_query_schema",
        lambda *_args: (_ for _ in ()).throw(KeyboardInterrupt("cancelled")),
    )

    def fail_cleanup(*_args: Any, **kwargs: Any) -> None:
        cleanup_calls.append(kwargs["drop_created_target"])
        raise RuntimeError("cleanup must not mask cancellation")

    monkeypatch.setattr(staged_attempt, "cleanup_stage", fail_cleanup)

    with pytest.raises(KeyboardInterrupt, match="cancelled"):
        staged_attempt.run_staged_source_transfer_attempt(
            _staged_options(),
            insert_retry_cnt=1,
        )

    assert cleanup_calls == [True]
    assert all(connection.closed for connection in connections)


def test_keyed_source_staging_pipelines_ready_key_before_later_ctas_finishes(
    monkeypatch: Any,
) -> None:
    slices = [
        TransferSlice(0, (1,), "", "SELECT 1 AS id", "key=1"),
        TransferSlice(1, (2,), "", "SELECT 2 AS id", "key=2"),
    ]
    options = _staged_options(
        transfer_slices=slices,
        transfer_keys=["key"],
        transfer_concurrency=TransferConcurrency(None, 2, 1, 2, 1, True),
    )
    internal_columns = resolve_internal_columns(["id"], "gp")
    metadata = staged_keyed_pipeline.freeze_attempt_metadata(
        source_columns=["id"],
        source_column_types={"id": "bigint"},
        stage_column_types={"id": "BIGINT"},
        internal_columns=internal_columns,
    )
    state = TransferStageState(
        target_exists=True,
        source_columns=["id"],
        internal_columns=internal_columns,
    )
    runtime = staged_keyed_pipeline.LazyKeyedRuntime(
        slices,
        read_workers=2,
        write_workers=1,
    )
    progress = staged_keyed_pipeline.TransferProgressTracker(
        total_key_count=2,
        active_writers=1,
    )
    source_connections = staged_keyed_pipeline.BoundedConnectionManager(
        "source",
        2,
        role="test source pool",
        open_connection=lambda _key: object(),
    )
    target_connections = staged_keyed_pipeline.BoundedConnectionManager(
        "target",
        1,
        role="test target pool",
        open_connection=lambda _key: object(),
    )
    events: list[str] = []
    later_ctas_started = threading.Event()
    release_later_ctas = threading.Event()
    later_ctas_finished = threading.Event()

    monkeypatch.setattr(staged_keyed_pipeline, "time_print", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(staged_keyed_pipeline, "log_batch_progress", lambda *_args: None)
    monkeypatch.setattr(staged_keyed_pipeline, "log_key_verification", lambda *_args: None)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "allocate_source_stage_name",
        lambda _options, _ref, slice_index: f"source_{slice_index}",
    )

    def materialize(_options: Any, _ref: Any, _metadata: Any, item: Any, _stage: str) -> int:
        events.append(f"ctas-start:{item.index}")
        if item.index == 1:
            later_ctas_started.set()
            assert release_later_ctas.wait(timeout=5)
            later_ctas_finished.set()
        events.append(f"ctas-complete:{item.index}")
        return 1

    monkeypatch.setattr(
        staged_keyed_pipeline,
        "materialize_source_key",
        materialize,
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "create_target_writer_stage",
        lambda *_args, **_kwargs: events.append("target-stage-created") or "target_0",
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "read_key_batch",
        lambda _options, _ref, task, _metadata, start, stop, **_kwargs: RowBatch(
            ["id"],
            [(task.transfer_slice.index,) for _ in range(start, stop)],
        ),
    )

    def insert(
        _options: Any,
        _ref: Any,
        _stage: str,
        batch: Any,
        _metadata: Any,
        **_kwargs: Any,
    ) -> int:
        if batch.task.transfer_slice.index == 0:
            assert later_ctas_started.wait(timeout=5)
            assert not later_ctas_finished.is_set()
            events.append("insert:0")
            release_later_ctas.set()
        else:
            events.append("insert:1")
        return batch.batch.row_count

    monkeypatch.setattr(staged_keyed_pipeline, "insert_target_batch", insert)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "validate_target_key",
        lambda _options, _ref, _metadata, task, _stage, _rows: events.append(
            f"validate:{task.transfer_slice.index}"
        ),
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "drop_source_stage",
        lambda _options, _ref, task: events.append(f"drop:{task.transfer_slice.index}"),
    )

    staged_keyed_pipeline._run_lazy_workers(
        options,
        metadata,
        state,
        runtime,
        source_connections,
        target_connections,
        progress,
        insert_retry_cnt=1,
    )

    assert events.index("insert:0") < events.index("ctas-complete:1")
    assert events.count("target-stage-created") == 1
    assert events.index("validate:0") < events.index("drop:0")
    assert events.index("validate:1") < events.index("drop:1")
    assert runtime.source_stage_tables == []
    assert set(runtime.verified) == {0, 1}


def test_lazy_source_staged_writers_keep_each_key_on_one_target_stage(
    monkeypatch: Any,
) -> None:
    slices = [
        TransferSlice(index, (index,), "", f"SELECT {index}", f"key={index}") for index in range(4)
    ]
    options = _staged_options(
        transfer_slices=slices,
        transfer_keys=["key"],
        batch_size=2,
        transfer_concurrency=TransferConcurrency(None, 2, 3, 2, 3, True),
    )
    internal_columns = resolve_internal_columns(["id"], "gp")
    metadata = staged_keyed_pipeline.freeze_attempt_metadata(
        source_columns=["id"],
        source_column_types={"id": "bigint"},
        stage_column_types={"id": "BIGINT"},
        internal_columns=internal_columns,
    )
    state = TransferStageState(
        target_exists=True,
        source_columns=["id"],
        internal_columns=internal_columns,
    )
    runtime = staged_keyed_pipeline.LazyKeyedRuntime(
        slices,
        read_workers=2,
        write_workers=3,
    )
    progress = staged_keyed_pipeline.TransferProgressTracker(
        total_key_count=4,
        active_writers=3,
    )
    source_connections = staged_keyed_pipeline.BoundedConnectionManager(
        "source",
        2,
        role="test source pool",
        open_connection=lambda _key: object(),
    )
    target_connections = staged_keyed_pipeline.BoundedConnectionManager(
        "target",
        3,
        role="test target pool",
        open_connection=lambda _key: object(),
    )
    expected_rows = {0: 3, 1: 0, 2: 3, 3: 0}
    stages_by_key: dict[int, set[str]] = {}
    keys_by_writer: dict[int, list[int]] = {}
    created_stages: dict[int, str] = {}

    monkeypatch.setattr(staged_keyed_pipeline, "time_print", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(staged_keyed_pipeline, "log_batch_progress", lambda *_args: None)
    monkeypatch.setattr(staged_keyed_pipeline, "log_key_verification", lambda *_args: None)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "allocate_source_stage_name",
        lambda _options, _ref, slice_index: f"source_{slice_index}",
    )
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "materialize_source_key",
        lambda _options, _ref, _metadata, item, _stage: expected_rows[item.index],
    )

    def create_stage(
        _options: Any,
        _ref: Any,
        _metadata: Any,
        writer_index: int,
        **_kwargs: Any,
    ) -> str:
        stage = f"target_{writer_index}"
        assert writer_index not in created_stages
        created_stages[writer_index] = stage
        return stage

    monkeypatch.setattr(staged_keyed_pipeline, "create_target_writer_stage", create_stage)
    monkeypatch.setattr(
        staged_keyed_pipeline,
        "read_key_batch",
        lambda _options, _ref, task, _metadata, start, stop, **_kwargs: RowBatch(
            ["id"],
            [(task.transfer_slice.index,) for _ in range(start, stop)],
        ),
    )

    def insert(
        _options: Any,
        _ref: Any,
        stage_table: str,
        batch: Any,
        _metadata: Any,
        **_kwargs: Any,
    ) -> int:
        key = batch.task.transfer_slice.index
        stages_by_key.setdefault(key, set()).add(stage_table)
        return batch.batch.row_count

    def validate(
        _options: Any,
        _ref: Any,
        _metadata: Any,
        task: Any,
        stage_table: str | None,
        streamed_rows: int,
    ) -> None:
        key = task.transfer_slice.index
        writer = task.writer_index
        assert writer is not None
        assert streamed_rows == expected_rows[key]
        keys_by_writer.setdefault(writer, []).append(key)
        if expected_rows[key]:
            assert stage_table == f"target_{writer}"

    monkeypatch.setattr(staged_keyed_pipeline, "insert_target_batch", insert)
    monkeypatch.setattr(staged_keyed_pipeline, "validate_target_key", validate)
    monkeypatch.setattr(staged_keyed_pipeline, "drop_source_stage", lambda *_args: None)

    staged_keyed_pipeline._run_lazy_workers(
        options,
        metadata,
        state,
        runtime,
        source_connections,
        target_connections,
        progress,
        insert_retry_cnt=1,
    )

    assert set(runtime.verified) == set(expected_rows)
    assert set(stages_by_key) == {0, 2}
    assert all(len(stage_names) == 1 for stage_names in stages_by_key.values())
    assert created_stages == {
        writer: f"target_{writer}"
        for writer, keys in keys_by_writer.items()
        if any(expected_rows[key] for key in keys)
    }
    assert runtime.target_stages == created_stages


def test_materialize_snapshot_builds_all_slices_and_drops_partial_on_failure(
    monkeypatch: Any,
) -> None:
    slices = [
        TransferSlice(0, (1,), "id = 1", "SELECT 1 AS id", "one"),
        TransferSlice(1, (2,), "id = 2", "SELECT 2 AS id", "two"),
    ]
    options = _staged_options(transfer_slices=slices)
    state = TransferStageState(
        target_exists=True,
        source_columns=["id"],
        internal_columns=resolve_internal_columns(["id"], "gp"),
    )
    commands: list[str] = []
    dropped: list[str] = []
    adapter = SimpleNamespace(
        execute_command=lambda _connection, sql: commands.append(sql),
        drop_table=lambda _connection, table, **_kwargs: dropped.append(table),
    )
    monkeypatch.setattr(staged_attempt, "_allocate_snapshot_name", lambda *_args: "snap")
    monkeypatch.setattr(staged_attempt, "get_backend_adapter", lambda _backend: adapter)
    monkeypatch.setattr(
        staged_attempt,
        "execute_transfer_materialization",
        lambda _adapter, _backend, _connection, sql: commands.append(sql),
    )
    monkeypatch.setattr(
        staged_attempt,
        "_snapshot_slice_counts",
        lambda *_args: {0: 1, 1: 1},
    )

    assert staged_attempt._materialize_snapshot(options, {"connection": object()}, state) == (
        "snap",
        {0: 1, 1: 1},
    )
    assert any(sql.startswith("INSERT INTO snap") for sql in commands)
    assert any(sql.startswith("CREATE INDEX") for sql in commands)

    calls = 0

    def fail_second(*_args: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("slice insert failed")

    monkeypatch.setattr(staged_attempt, "execute_transfer_materialization", fail_second)
    with pytest.raises(OSError, match="slice insert failed"):
        staged_attempt._materialize_snapshot(options, {"connection": object()}, state)
    assert dropped == ["snap"]

    monkeypatch.setattr(
        staged_attempt,
        "execute_transfer_materialization",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        staged_attempt,
        "_snapshot_slice_counts",
        lambda *_args: (_ for _ in ()).throw(KeyboardInterrupt("count cancelled")),
    )

    def fail_drop(_connection: Any, table: str, **_kwargs: Any) -> None:
        dropped.append(table)
        raise RuntimeError("snapshot cleanup failed")

    adapter.drop_table = fail_drop
    dropped.clear()
    with pytest.raises(KeyboardInterrupt, match="count cancelled"):
        staged_attempt._materialize_snapshot(options, {"connection": object()}, state)
    assert dropped == ["snap"]

    with pytest.raises(RuntimeError, match="configuration is incomplete"):
        staged_attempt._materialize_snapshot(
            _staged_options(source_transfer_staging_schema=None),
            {"connection": object()},
            TransferStageState(target_exists=True),
        )

    monkeypatch.setattr(
        staged_attempt,
        "build_snapshot_select_sql",
        lambda **_kwargs: (_ for _ in ()).throw(ValueError("render failed")),
    )
    dropped.clear()
    with pytest.raises(ValueError, match="render failed"):
        staged_attempt._materialize_snapshot(options, {"connection": object()}, state)
    assert not dropped


def test_snapshot_name_counts_and_worker_stage_allocation(monkeypatch: Any) -> None:
    options = _staged_options()
    existence = iter([True, False])
    monkeypatch.setattr(staged_attempt, "table_exists", lambda *_args, **_kwargs: next(existence))
    monkeypatch.setattr(
        staged_attempt,
        "build_stage_table_name",
        lambda _backend, _target, **kwargs: str(kwargs["random_suffix"]),
    )
    allocated = staged_attempt._allocate_snapshot_name(options, {"connection": object()})
    assert allocated != f"{options.transfer_id}__source"

    monkeypatch.setattr(staged_attempt, "table_exists", lambda *_args, **_kwargs: True)
    with pytest.raises(RuntimeError, match="unique source snapshot"):
        staged_attempt._allocate_snapshot_name(options, {"connection": object()})

    adapter = SimpleNamespace(quote_identifier=lambda value: f'"{value}"')
    monkeypatch.setattr(staged_attempt, "get_backend_adapter", lambda _backend: adapter)
    monkeypatch.setattr(
        staged_attempt,
        "_read_backend",
        lambda *_args, **_kwargs: SimpleNamespace(columns=([0, 2], [3, 1])),
    )
    assert staged_attempt._snapshot_slice_counts(options, object(), "snap", "slice") == {0: 3}

    state = TransferStageState(
        target_exists=True,
        source_columns=["id"],
        stage_column_types={"id": "BIGINT"},
        internal_columns=resolve_internal_columns(["id"], "gp"),
    )
    monkeypatch.setattr(
        staged_attempt,
        "create_stage_table",
        lambda *_args, **kwargs: f"stage_{kwargs['random_suffix']}",
    )
    tables = staged_attempt._create_worker_stages(
        options,
        {"connection": object()},
        state,
        worker_count=2,
    )
    assert len(tables) == 2
    assert state.stage_table == tables[0]


@pytest.mark.parametrize(
    ("ambiguous_candidate", "expected_registered"),
    [
        (None, ["stage.worker_0"]),
        ("stage.ambiguous_worker_1", ["stage.worker_0", "stage.ambiguous_worker_1"]),
    ],
)
def test_worker_stage_creation_registers_partial_and_ambiguous_candidates(
    monkeypatch: Any,
    ambiguous_candidate: str | None,
    expected_registered: list[str],
) -> None:
    options = _staged_options()
    state = TransferStageState(
        target_exists=True,
        source_columns=["id"],
        stage_column_types={"id": "BIGINT"},
        internal_columns=resolve_internal_columns(["id"], "gp"),
    )
    call_count = 0

    def create(*_args: Any, **kwargs: Any) -> str:
        nonlocal call_count
        callback = kwargs["on_stage_candidate"]
        if call_count == 0:
            call_count += 1
            callback("stage.worker_0")
            return "stage.worker_0"
        if ambiguous_candidate is not None:
            callback(ambiguous_candidate)
        raise OSError("worker stage create failed")

    monkeypatch.setattr(staged_attempt, "create_stage_table", create)

    with pytest.raises(OSError, match="worker stage create failed"):
        staged_attempt._create_worker_stages(
            options,
            {"connection": object()},
            state,
            worker_count=2,
        )

    assert state.stage_table == "stage.worker_0"
    assert state.stage_tables == expected_registered
    assert state.stage_table_created is True


def test_worker_stage_creation_drops_ambiguous_candidate_after_success(
    monkeypatch: Any,
) -> None:
    options = _staged_options()
    state = TransferStageState(
        target_exists=True,
        source_columns=["id"],
        stage_column_types={"id": "BIGINT"},
        internal_columns=resolve_internal_columns(["id"], "gp"),
    )
    dropped: list[str] = []

    def create(*_args: Any, **kwargs: Any) -> str:
        callback = kwargs["on_stage_candidate"]
        callback("stage.ambiguous")
        callback("stage.worker_0")
        return "stage.worker_0"

    monkeypatch.setattr(staged_attempt, "create_stage_table", create)
    monkeypatch.setattr(
        staged_attempt,
        "cleanup_stage_table",
        lambda _backend, _connection, table, **_kwargs: dropped.append(table),
    )

    assert staged_attempt._create_worker_stages(
        options,
        {"connection": object()},
        state,
        worker_count=1,
    ) == ["stage.worker_0"]
    assert dropped == ["stage.ambiguous"]
    assert state.stage_table == "stage.worker_0"
    assert state.stage_tables == ["stage.worker_0"]


@pytest.mark.parametrize(
    ("total_rows", "batch_size", "requested", "expected"),
    [
        (0, 100, 3, 1),
        (20, 100, 3, 1),
        (100, 100, 3, 1),
        (201, 100, 5, 3),
        (1_000, 100, 3, 3),
    ],
)
def test_effective_transfer_worker_count_uses_initial_batch_count(
    total_rows: int,
    batch_size: int,
    requested: int,
    expected: int,
) -> None:
    assert (
        staged_attempt._effective_transfer_worker_count(
            requested,
            total_rows,
            batch_size,
        )
        == expected
    )


def test_range_worker_retries_only_failed_interval_and_validates_size(monkeypatch: Any) -> None:
    options = _staged_options()
    state = TransferStageState(
        target_exists=True,
        stage_column_types={"id": "BIGINT"},
    )
    connections = [object(), object(), object(), object()]
    monkeypatch.setattr(staged_attempt, "get_sql_connection", lambda _key: connections.pop(0))
    monkeypatch.setattr(staged_attempt, "close_connection_ref", lambda *_args: None)
    monkeypatch.setattr(staged_attempt, "rollback_quietly", lambda _connection: None)
    monkeypatch.setattr(staged_attempt, "replace_connection", lambda *_args: None)
    inserted: list[int] = []
    monkeypatch.setattr(
        staged_attempt,
        "insert_rows_batch",
        lambda *_args, **_kwargs: inserted.append(len(_args[4])),
    )
    attempts = 0

    def flaky_read(
        _options: TransferOptions,
        _connection: Any,
        _snapshot: str,
        _columns: list[str],
        _state: TransferStageState,
        claimed: OrdinalRange,
    ) -> RowBatch:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("temporary source failure")
        return RowBatch(["id"], [(value,) for value in range(claimed.row_count)])

    monkeypatch.setattr(staged_attempt, "_read_snapshot_range", flaky_read)
    scheduler = AdaptiveRangeScheduler({0: 2})
    staged_attempt._range_worker(options, "snap", ["id"], state, "stage", scheduler, 0, 1)
    scheduler.validate_complete()
    assert inserted == [1, 1]

    monkeypatch.setattr(staged_attempt, "get_sql_connection", lambda _key: object())
    monkeypatch.setattr(
        staged_attempt,
        "_read_snapshot_range",
        lambda *_args: RowBatch(["id"], []),
    )
    with pytest.raises(RuntimeError, match="returned 0 row"):
        staged_attempt._range_worker(
            options,
            "snap",
            ["id"],
            state,
            "stage",
            AdaptiveRangeScheduler({0: 1}),
            0,
            1,
        )

    monkeypatch.setattr(
        staged_attempt,
        "_read_snapshot_range",
        lambda *_args: (_ for _ in ()).throw(OSError("terminal")),
    )
    with pytest.raises(OSError, match="terminal"):
        staged_attempt._range_worker(
            options,
            "snap",
            ["id"],
            state,
            "stage",
            AdaptiveRangeScheduler({0: 1}),
            0,
            1,
        )


def test_range_read_workers_and_consolidation(monkeypatch: Any) -> None:
    options = _staged_options()
    internal = resolve_internal_columns(["id"], "gp")
    state = TransferStageState(
        target_exists=True,
        internal_columns=internal,
        source_column_types={"id": "bigint"},
        stage_column_types={"id": "BIGINT"},
    )
    adapter = SimpleNamespace(
        normalize_transfer_source_batch=lambda batch, _types: batch,
    )
    monkeypatch.setattr(staged_attempt, "get_backend_adapter", lambda _backend: adapter)
    monkeypatch.setattr(
        staged_attempt,
        "_read_backend",
        lambda *_args, **_kwargs: SimpleNamespace(column_names=("id",), columns=([7],)),
    )
    batch = staged_attempt._read_snapshot_range(
        options,
        object(),
        "snap",
        ["id"],
        state,
        OrdinalRange(0, 1, 2),
    )
    assert batch.rows == [(7,)]

    state.internal_columns = None
    with pytest.raises(RuntimeError, match="internal columns"):
        staged_attempt._read_snapshot_range(
            options,
            object(),
            "snap",
            ["id"],
            state,
            OrdinalRange(0, 1, 2),
        )
    state.internal_columns = internal

    inserted: list[tuple[str, str]] = []
    monkeypatch.setattr(
        staged_attempt,
        "insert_from_table",
        lambda _backend, _connection, target, source, **_kwargs: inserted.append((target, source)),
    )
    refreshed: list[str] = []
    monkeypatch.setattr(
        staged_attempt,
        "replace_connection",
        lambda connection_key, _ref: refreshed.append(connection_key),
    )
    staged_attempt._consolidate_worker_stages(
        options,
        {"connection": object()},
        state,
        ["stage_0", "stage_1", "stage_2"],
    )
    assert inserted == [("stage_0", "stage_1"), ("stage_0", "stage_2")]
    assert refreshed == ["target"]
    staged_attempt._consolidate_worker_stages(
        _staged_options(write_mode="upsert"),
        {"connection": object()},
        state,
        ["stage_0", "stage_1"],
    )
    assert len(inserted) == 2
    assert refreshed == ["target"]

    ran: list[int] = []
    monkeypatch.setattr(
        staged_attempt,
        "_range_worker",
        lambda *_args: ran.append(int(_args[-2])),
    )
    staged_attempt._run_range_workers(
        options,
        "snap",
        ["id"],
        state,
        ["stage_0", "stage_1"],
        AdaptiveRangeScheduler({}),
        insert_retry_cnt=1,
    )
    assert sorted(ran) == [0, 1]

    monkeypatch.setattr(
        staged_attempt,
        "_range_worker",
        lambda *_args: (_ for _ in ()).throw(OSError("worker failed")),
    )
    with pytest.raises(OSError, match="worker failed"):
        staged_attempt._run_range_workers(
            options,
            "snap",
            ["id"],
            state,
            ["stage_0"],
            AdaptiveRangeScheduler({}),
            insert_retry_cnt=1,
        )


def test_transfer_stage_backend_helpers_cover_storage_and_identifier_edges() -> None:
    calls: list[str] = []
    adapter = SimpleNamespace(
        execute_materialization_command=lambda _connection, sql: calls.append(f"trino:{sql}"),
        execute_command=lambda _connection, sql: calls.append(f"other:{sql}"),
    )
    transfer_stage.execute_transfer_materialization(adapter, "trino", object(), "CREATE")
    transfer_stage.execute_transfer_materialization(adapter, "gp", object(), "CREATE")
    assert calls == ["trino:CREATE", "other:CREATE"]
    assert transfer_stage.normalize_unquoted_identifier("MiXeD", "gp") == "mixed"
    assert transfer_stage.normalize_unquoted_identifier("MiXeD", "ch") == "MiXeD"
    with pytest.raises(KeyError):
        transfer_stage.normalize_unquoted_identifier("x", "unknown")

    assert transfer_stage.build_transfer_stage_tail("gp", "user", "suffix") == "suffix"
    assert transfer_stage.build_transfer_stage_tail("trino", "user", "suffix") == "suffix"
    assert transfer_stage.build_transfer_stage_tail("ch", None, "suffix") == "suffix"
    with pytest.raises(KeyError):
        transfer_stage.build_transfer_stage_tail("unknown", None, "suffix")
    assert transfer_stage.collision_stage_suffix("gp", "base", "12345678") == "base12345"
    assert transfer_stage.collision_stage_suffix("ch", "base", "12345678") == "base12345"
    with pytest.raises(KeyError):
        transfer_stage.collision_stage_suffix("unknown", "base", "123")

    expected_name = transfer_stage.fit_hashed_stage_identifier("gp", "hash__", "name", "__tail")
    assert (
        transfer_stage.fit_hashed_stage_identifier("trino", "hash__", "name", "__tail")
        == expected_name
    )
    stage_tail = "a" * 32 + "__w00000"
    trino_name = transfer_stage.fit_hashed_stage_identifier(
        "trino",
        "f" * 16 + "__",
        "destination_" * 20,
        stage_tail,
    )
    assert len(trino_name.encode()) <= 63
    assert trino_name.startswith("f" * 16 + "__")
    assert trino_name.endswith(stage_tail)
    with pytest.raises(ValueError, match="too long for Greenplum"):
        transfer_stage.fit_hashed_stage_identifier(
            "trino",
            "x" * 64,
            "name",
            "tail",
        )
    gp_name = transfer_stage.fit_hashed_stage_identifier(
        "gp",
        "hash__",
        "😀" * 100,
        "__tail",
    )
    assert len(gp_name.encode()) <= 63
    with pytest.raises(ValueError, match="too long"):
        transfer_stage.fit_hashed_stage_identifier("gp", "x" * 64, "name", "tail")
    with pytest.raises(KeyError):
        transfer_stage.fit_hashed_stage_identifier("unknown", "hash__", "name", "tail")
    with pytest.raises(KeyError):
        load_stage._stage_base_identifier("unknown", "name", None, "suffix")

    gp_sql, gp_post = transfer_stage.build_source_snapshot_sqls(
        "gp", "snap", "SELECT 1", "slice", "ordinal"
    )
    ch_sql, ch_post = transfer_stage.build_source_snapshot_sqls(
        "ch", "snap", "SELECT 1", "slice", "ordinal"
    )
    trino_sql, trino_post = transfer_stage.build_source_snapshot_sqls(
        "trino", "snap", "SELECT 1", "slice", "ordinal"
    )
    assert "DISTRIBUTED RANDOMLY" in gp_sql and len(gp_post) == 2
    assert "MergeTree" in ch_sql and not ch_post
    assert trino_sql == "CREATE TABLE snap AS SELECT 1" and not trino_post
    assert source_count._apply_query_label("SELECT 1", None) == "SELECT 1"


def test_range_scheduler_rejects_invalid_ownership_and_incomplete_coverage() -> None:
    for values in [(-1, 1, 2), (0, 0, 2), (0, 2, 2)]:
        with pytest.raises(ValueError):
            OrdinalRange(*values)
    with pytest.raises(ValueError, match="non-negative"):
        AdaptiveRangeScheduler({-1: 2})

    scheduler = AdaptiveRangeScheduler({0: 3})
    with pytest.raises(ValueError, match="worker_id"):
        scheduler.claim(-1, 1)
    with pytest.raises(ValueError, match="batch_size"):
        scheduler.claim(0, 0)
    first = scheduler.claim(0, 1)
    assert first is not None and not scheduler.finished
    with pytest.raises(RuntimeError, match="not claimed"):
        scheduler.complete(1, first)
    with pytest.raises(ValueError, match="reduced_batch_size"):
        scheduler.requeue_failed(0, first, reduced_batch_size=0)
    with pytest.raises(RuntimeError, match="not claimed"):
        scheduler.requeue_failed(1, first, reduced_batch_size=1)
    with pytest.raises(RuntimeError, match="incomplete ranges"):
        scheduler.validate_complete()
    scheduler.complete(0, first)
    second = scheduler.claim(0, 3)
    assert second is not None
    scheduler.complete(0, second)
    assert scheduler.finished

    gap = AdaptiveRangeScheduler({0: 2})
    gap._pending.clear()
    gap._completed.add(OrdinalRange(0, 2, 3))
    with pytest.raises(RuntimeError, match="gap or overlap"):
        gap.validate_complete()
    incomplete = AdaptiveRangeScheduler({0: 2})
    incomplete._pending.clear()
    with pytest.raises(RuntimeError, match="incomplete"):
        incomplete.validate_complete()


def test_internal_identity_quotes_and_rejects_mixed_runtime_values() -> None:
    internal = resolve_internal_columns([], "trino")
    assert all(value.startswith('"') for value in internal.quoted("trino"))
    assert_transfer_identity(
        expected_transfer_id="a",
        actual_transfer_id="a",
        expected_destination="target",
        actual_destination="target",
        resource="stage",
    )
    with pytest.raises(RuntimeError, match="transfer ID"):
        assert_transfer_identity(
            expected_transfer_id="a",
            actual_transfer_id="b",
            expected_destination="target",
            actual_destination="target",
            resource="stage",
        )
    with pytest.raises(RuntimeError, match="destination"):
        assert_transfer_identity(
            expected_transfer_id="a",
            actual_transfer_id="a",
            expected_destination="target",
            actual_destination="other",
            resource="stage",
        )


def test_stage_validation_checks_user_payload_count(monkeypatch: Any) -> None:
    options = _staged_options()
    internal = resolve_internal_columns(["id"], "gp")
    monkeypatch.setattr(stage_validation, "count_table_rows", lambda *_args, **_kwargs: 2)
    stage_validation.validate_transfer_stage_identity(
        options=options,
        connection=object(),
        stage_tables=["stage"],
        internal_columns=internal,
        expected_slice_counts={0: 2, 1: 0},
    )
    monkeypatch.setattr(stage_validation, "count_table_rows", lambda *_args, **_kwargs: 1)
    with pytest.raises(RuntimeError, match="payload count"):
        stage_validation.validate_transfer_stage_identity(
            options=options,
            connection=object(),
            stage_tables=["stage"],
            internal_columns=internal,
            expected_slice_counts={0: 2},
        )


def test_empty_slice_requires_no_target_stage_query() -> None:
    stage_validation.validate_transfer_stage_slice(
        options=_staged_options(),
        connection=object(),
        stage_table=[],
        internal_columns=resolve_internal_columns(["id"], "gp"),
        slice_id=3,
        expected_count=0,
        streamed_count=0,
    )


def test_nonempty_stage_slice_accepts_exact_in_memory_count() -> None:
    stage_validation.validate_transfer_stage_slice(
        options=_staged_options(),
        connection=object(),
        stage_table="target_stage.writer_0",
        internal_columns=resolve_internal_columns(["id"], "gp"),
        slice_id=3,
        expected_count=2,
        streamed_count=2,
    )


def test_stage_slice_validation_rejects_in_memory_count_mismatch() -> None:
    with pytest.raises(RuntimeError, match="streamed"):
        stage_validation.validate_transfer_stage_slice(
            options=_staged_options(),
            connection=object(),
            stage_table="target_stage.writer_0",
            internal_columns=resolve_internal_columns(["id"], "gp"),
            slice_id=3,
            expected_count=2,
            streamed_count=1,
        )


def test_superseded_cleanup_preserves_unverifiable_and_current_stages(monkeypatch: Any) -> None:
    internal = resolve_internal_columns(["id"], "gp")
    options = _staged_options()
    assert (
        cleanup_superseded_transfer_stages(
            options=options,
            connection=object(),
            backend="gp",
            connection_key="target",
            staging_schema=None,
            internal_columns=internal,
        )
        == []
    )

    adapter = SimpleNamespace(
        query_transfer_stage_table_names=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("discovery failed")
        ),
    )
    monkeypatch.setattr(
        "analytics_toolkit.sql.dml.transfer.flow.superseded.get_backend_adapter",
        lambda _backend: adapter,
    )
    assert (
        cleanup_superseded_transfer_stages(
            options=options,
            connection=object(),
            backend="gp",
            connection_key="target",
            staging_schema="stage",
            internal_columns=internal,
        )
        == []
    )

    candidate = f"{options.destination_hash}__target__stage__{'b' * 32}"
    adapter = SimpleNamespace(
        query_transfer_stage_table_names=lambda *_args, **_kwargs: [
            "wrong_prefix__" + "b" * 32,
            f"{options.destination_hash}__legacy",
            candidate,
        ],
        qualify_transfer_stage_table_name=lambda _key, schema, table: f"{schema}.{table}",
        quote_identifier=lambda value: f'"{value}"',
    )
    monkeypatch.setattr(
        "analytics_toolkit.sql.dml.transfer.flow.superseded.get_backend_adapter",
        lambda _backend: adapter,
    )
    assert (
        cleanup_superseded_transfer_stages(
            options=options,
            connection=object(),
            backend="gp",
            connection_key="target",
            staging_schema="stage",
            internal_columns=internal,
        )
        == []
    )

    current_empty = f"{options.destination_hash}__target__stage__{options.transfer_id}__s00000"
    adapter.query_transfer_stage_table_names = lambda *_args, **_kwargs: [current_empty]
    dropped: list[str] = []
    monkeypatch.setattr(
        superseded,
        "cleanup_stage_table",
        lambda _backend, _connection, table, **_kwargs: dropped.append(table),
    )
    assert cleanup_superseded_transfer_stages(
        options=options,
        connection=object(),
        backend="gp",
        connection_key="source",
        staging_schema="stage",
        internal_columns=internal,
        include_current_transfer_id=True,
    ) == [f"stage.{current_empty}"]
    assert dropped == [f"stage.{current_empty}"]

    stale_empty = f"{options.destination_hash}__target__stage__{'b' * 32}__s00000"
    adapter.query_transfer_stage_table_names = lambda *_args, **_kwargs: [stale_empty]
    dropped.clear()
    assert cleanup_superseded_transfer_stages(
        options=options,
        connection=object(),
        backend="gp",
        connection_key="target",
        staging_schema="stage",
        internal_columns=internal,
    ) == [f"stage.{stale_empty}"]
    assert dropped == [f"stage.{stale_empty}"]

    adapter.query_transfer_stage_table_names = lambda *_args, **_kwargs: [current_empty]
    assert (
        cleanup_superseded_transfer_stages(
            options=options,
            connection=object(),
            backend="gp",
            connection_key="target",
            staging_schema="stage",
            internal_columns=internal,
        )
        == []
    )


def test_new_transfer_dispatch_and_identity_guard_branches(monkeypatch: Any) -> None:
    options = _staged_options(concurrency=4)
    monkeypatch.setattr(
        attempt,
        "run_staged_source_transfer_attempt",
        lambda _options, **_kwargs: 7,
    )
    assert attempt.run_transfer_attempt(options, 1, 1) == 7
    assert dry_run.dry_run_worker_stage_count(options) == 4

    state = TransferStageState(target_exists=True)
    assert parquet_batches.append_transfer_identity_columns(
        RowBatch(["id"], [(1,)]),
        options=_staged_options(transfer_id=None),
        stage_state=state,
        slice_id=0,
        start_ordinal=1,
    ).rows == [(1,)]
    unchanged = parquet_batches.append_transfer_identity_columns(
        RowBatch(["id"], [(1,)]),
        options=options,
        stage_state=state,
        slice_id=0,
        start_ordinal=1,
    )
    assert unchanged.columns == ["id"]
    assert unchanged.rows == [(1,)]

    attempt._cleanup_target_superseded_stages(options, state)


def test_transfer_parquet_filename_contains_runtime_range(monkeypatch: Any) -> None:
    uploaded: list[str] = []
    monkeypatch.setattr(
        parquet_stage,
        "row_batch_to_arrow_table",
        lambda _pa, _batch, **_kwargs: object(),
    )
    monkeypatch.setattr(
        parquet_stage,
        "write_arrow_table_to_parquet",
        lambda _pq, _table, stream, **_kwargs: stream.write(b"parquet"),
    )
    monkeypatch.setattr(
        parquet_stage,
        "upload_spooled_file",
        lambda _fsspec, _stream, uri: uploaded.append(uri),
    )
    rows = parquet_stage.write_batch_to_parquet_stage(
        RowBatch(["id"], [(1,)]),
        file_index=3,
        slice_index=None,
        stage_external_location="memory://bucket/stage/",
        pa=object(),
        pq=object(),
        fsspec_module=object(),
        row_group_size=10,
        transfer_id="a" * 32,
        worker_id=2,
        start_ordinal=10,
        stop_ordinal=20,
    )
    assert rows == 1
    assert "worker-00002-slice-00000-range-00000000000000000010-00000000000000000020" in uploaded[0]

    adapter = SimpleNamespace(
        infer_parquet_stage_column_types_from_rows=lambda _batch: {"id": "BIGINT"}
    )
    monkeypatch.setattr(parquet_stage, "get_backend_adapter", lambda _backend: adapter)
    assert parquet_stage.infer_trino_column_types_from_rows(RowBatch(["id"], [(1,)])) == {
        "id": "BIGINT"
    }
    assert list(parquet_stage.sample_dataframe_from_batch(RowBatch(["id"], [(1,)])).columns) == [
        "id"
    ]

    monkeypatch.setattr(
        transfer_schema,
        "get_backend_adapter",
        lambda _backend: SimpleNamespace(map_source_type_to_target=lambda column: column.name),
    )
    assert transfer_schema.map_source_type_to_target(SourceColumn("id"), "gp") == "id"


def test_range_worker_coordinator_cancels_pending_future(monkeypatch: Any) -> None:
    cancelled: list[bool] = []

    class FailedFuture:
        def exception(self) -> OSError:
            return OSError("worker failed")

    class PendingFuture:
        def cancel(self) -> None:
            cancelled.append(True)

    monkeypatch.setattr(staged_attempt, "_range_worker", lambda *_args: None)
    monkeypatch.setattr(
        staged_attempt,
        "wait",
        lambda _pending, **_kwargs: ({FailedFuture()}, {PendingFuture()}),
    )
    with pytest.raises(OSError, match="worker failed"):
        staged_attempt._run_range_workers(
            _staged_options(),
            "snap",
            ["id"],
            TransferStageState(target_exists=True),
            ["stage_0"],
            AdaptiveRangeScheduler({}),
            insert_retry_cnt=1,
        )
    assert cancelled == [True]
