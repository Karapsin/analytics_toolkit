from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest
from analytics_toolkit.sql.backends.ch import wait as ch_wait
from analytics_toolkit.sql.backends.ch.creation_policy import ClickHouseCreationPolicy
from analytics_toolkit.sql.dml.transfer.flow import finalize
from analytics_toolkit.sql.dml.transfer.runtime.models import (
    TransferConnectionRefs,
    TransferOptions,
    TransferStageState,
)


def _options(*, write_mode: str = "append", retry_cnt: int = 2) -> TransferOptions:
    return TransferOptions(
        from_db_key="trino",
        from_db_backend="trino",
        to_db_key="ch",
        to_db_backend="ch",
        source_sql="select 1 as id",
        target_table="sandbox.target",
        write_mode=write_mode,
        replace_target_table=write_mode != "append",
        retry_cnt=retry_cnt,
        timeout_increment=0,
        regular_ch_policy=ClickHouseCreationPolicy(
            create_distributed_pair=True,
            shard_engine="ReplicatedMergeTree",
            shard_on_cluster="core",
            distributed_engine_template=(
                "Distributed({cluster}, {database}, {shard_table}, {sharding_key})"
            ),
            distributed_cluster="core",
            distributed_on_cluster="{cluster}",
            sharding_key="rand()",
            ddl_ready_timeout_seconds=300,
            ddl_ready_timeout_extension_cnt=1,
        ),
    )


def _stage_state(*, target_exists: bool) -> TransferStageState:
    return TransferStageState(
        target_exists=target_exists,
        target_existed_at_start=target_exists,
        stage_table="sandbox.stage",
        stage_table_created=True,
        first_non_empty_batch=pd.DataFrame({"id": [1]}),
        stage_column_types={"id": "Int64"},
        source_columns=["id"],
    )


@pytest.mark.parametrize(
    ("write_mode", "target_exists", "expected"),
    [
        ("replace", True, True),
        ("replace", False, True),
        ("append", False, True),
        ("upsert", False, True),
        ("append", True, False),
        ("upsert", True, False),
    ],
)
def test_fresh_target_retry_scope_depends_on_target_creation(
    write_mode: str,
    target_exists: bool,
    expected: bool,
) -> None:
    assert (
        finalize._creates_fresh_clickhouse_target(
            _options(write_mode=write_mode),
            _stage_state(target_exists=target_exists),
        )
        is expected
    )


def test_fresh_target_count_converges_without_rebuilding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = _options(retry_cnt=2)
    state = _stage_state(target_exists=False)
    target_counts = iter([4, 5])
    finalized_policies: list[ClickHouseCreationPolicy] = []
    drops: list[str] = []
    analyses: list[str] = []

    monkeypatch.setattr(
        finalize,
        "_run_with_fresh_target_connection",
        lambda _options, _role, operation: operation({"connection": object()}),
    )
    monkeypatch.setattr(finalize, "validate_stage_uniqueness", lambda **_kwargs: None)
    monkeypatch.setattr(finalize, "validate_stage_target_key_overlap", lambda **_kwargs: None)
    monkeypatch.setattr(
        finalize,
        "finalize_stage_table",
        lambda *_args, **kwargs: finalized_policies.append(kwargs["ch_creation_policy"]),
    )
    monkeypatch.setattr(finalize, "_count_loaded_stage_rows", lambda *_a, **_k: 5)
    monkeypatch.setattr(finalize, "count_table_rows", lambda *_a, **_k: next(target_counts))
    monkeypatch.setattr(finalize.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        finalize,
        "_drop_incomplete_fresh_target",
        lambda _options, **_kwargs: drops.append("target"),
    )
    monkeypatch.setattr(
        finalize,
        "analyze_table",
        lambda **_kwargs: analyses.append("target"),
    )

    finalize.finalize_loaded_stage(options, TransferConnectionRefs(), state, 5)

    assert len(finalized_policies) == 1
    assert all(policy.ddl_ready_timeout_extension_cnt == 1 for policy in finalized_policies)
    assert drops == []
    assert analyses == ["target"]
    assert state.target_created_by_operation is True


def test_fresh_target_count_rebuilds_after_readiness_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = _options(retry_cnt=2)
    state = _stage_state(target_exists=False)
    target_counts = iter([4, 5])
    finalized: list[str] = []
    drops: list[str] = []

    monkeypatch.setattr(
        finalize,
        "_run_with_fresh_target_connection",
        lambda _options, _role, operation: operation({"connection": object()}),
    )
    monkeypatch.setattr(finalize, "validate_stage_uniqueness", lambda **_kwargs: None)
    monkeypatch.setattr(finalize, "validate_stage_target_key_overlap", lambda **_kwargs: None)
    monkeypatch.setattr(
        finalize,
        "finalize_stage_table",
        lambda *_args, **_kwargs: finalized.append("target"),
    )
    monkeypatch.setattr(finalize, "_count_loaded_stage_rows", lambda *_a, **_k: 5)
    monkeypatch.setattr(finalize, "count_table_rows", lambda *_a, **_k: next(target_counts))
    monkeypatch.setattr(
        finalize,
        "run_ch_readiness_wait",
        lambda operation, **_kwargs: operation(0),
    )
    monkeypatch.setattr(
        finalize,
        "_drop_incomplete_fresh_target",
        lambda _options, **_kwargs: drops.append("target"),
    )
    monkeypatch.setattr(finalize, "analyze_table", lambda **_kwargs: None)

    finalize.finalize_loaded_stage(options, TransferConnectionRefs(), state, 5)

    assert finalized == ["target", "target"]
    assert drops == ["target"]


def test_fresh_target_count_query_error_can_converge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = _options()
    outcomes = iter([RuntimeError("replica unavailable"), 5])

    monkeypatch.setattr(
        finalize,
        "_run_with_fresh_target_connection",
        lambda _options, _role, operation: operation({"connection": object()}),
    )

    def count_rows(*_args: Any, **_kwargs: Any) -> int:
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(finalize, "count_table_rows", count_rows)
    monkeypatch.setattr(finalize.time, "sleep", lambda _seconds: None)

    assert (
        finalize._wait_for_fresh_target_row_count(
            options,
            5,
            target_connection_runner=None,
        )
        == 5
    )


def test_fresh_target_count_timeout_reports_last_observed_with_wait_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = _options()
    policy = options.regular_ch_policy
    assert policy is not None
    options = replace(
        options,
        regular_ch_policy=replace(
            policy,
            ddl_ready_timeout_seconds=0,
            ddl_ready_timeout_extension_cnt=0,
            ddl_wait_policy="wait_none",
        ),
    )
    monkeypatch.setattr(
        finalize,
        "_run_with_fresh_target_connection",
        lambda _options, _role, operation: operation({"connection": object()}),
    )
    monkeypatch.setattr(finalize, "count_table_rows", lambda *_a, **_k: 4)

    with pytest.raises(
        finalize.FreshTargetFinalizationRowCountMismatchError,
        match=r"target has 4 row\(s\), stage has 5 row\(s\)",
    ):
        finalize._wait_for_fresh_target_row_count(
            options,
            5,
            target_connection_runner=None,
        )


def test_fresh_target_count_timeout_preserves_query_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = _options()
    policy = options.regular_ch_policy
    assert policy is not None
    options = replace(
        options,
        regular_ch_policy=replace(
            policy,
            ddl_ready_timeout_seconds=0,
            ddl_ready_timeout_extension_cnt=0,
        ),
    )
    monkeypatch.setattr(
        finalize,
        "_run_with_fresh_target_connection",
        lambda _options, _role, operation: operation({"connection": object()}),
    )
    query_error = RuntimeError("replica unavailable")

    def count_rows(*_args: Any, **_kwargs: Any) -> int:
        raise query_error

    monkeypatch.setattr(finalize, "count_table_rows", count_rows)

    with pytest.raises(finalize.FreshTargetFinalizationRowCountMismatchError) as exc_info:
        finalize._wait_for_fresh_target_row_count(
            options,
            5,
            target_connection_runner=None,
        )

    assert exc_info.value.__cause__ is not None
    readiness_error = exc_info.value.__cause__.__cause__
    assert readiness_error is not None
    assert readiness_error.__cause__ is query_error


def test_existing_target_append_does_not_force_count_equality(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = _options(write_mode="append")
    state = _stage_state(target_exists=True)
    finalized: list[str] = []

    monkeypatch.setattr(
        finalize,
        "_run_with_fresh_target_connection",
        lambda _options, _role, operation: operation({"connection": object()}),
    )
    monkeypatch.setattr(finalize, "validate_stage_uniqueness", lambda **_kwargs: None)
    monkeypatch.setattr(finalize, "validate_stage_target_key_overlap", lambda **_kwargs: None)
    monkeypatch.setattr(finalize, "get_existing_target_insert_types", lambda *_a, **_k: {})
    monkeypatch.setattr(
        finalize,
        "finalize_stage_table",
        lambda *_args, **_kwargs: finalized.append("target"),
    )
    monkeypatch.setattr(finalize, "analyze_table", lambda **_kwargs: None)
    monkeypatch.setattr(
        finalize,
        "_validate_fresh_target_row_count",
        lambda *_a, **_k: pytest.fail("existing-target append must not compare total counts"),
    )

    finalize.finalize_loaded_stage(options, TransferConnectionRefs(), state, 1)

    assert finalized == ["target"]


def test_clickhouse_readiness_uses_each_timeout_extension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits: list[float] = []

    def wait_for_pair(*_args: Any, timeout_seconds: float, **_kwargs: Any) -> None:
        waits.append(timeout_seconds)
        if len(waits) < 3:
            message = "21/22 hosts ready"
            raise TimeoutError(message)

    monkeypatch.setattr(ch_wait, "_wait_for_ch_distributed_table_pair", wait_for_pair)
    policy = SimpleNamespace(
        shard_on_cluster="core",
        distributed_on_cluster="{cluster}",
        distributed_cluster="core",
        ddl_ready_timeout_seconds=300,
        ddl_ready_timeout_extension_cnt=2,
        ddl_ready_timeout_increment_seconds=60,
    )

    ch_wait.after_create_table(
        object(),
        object(),
        "sandbox.target",
        ch_distributed_table=True,
        ch_creation_policy=policy,
    )

    assert waits == [300, 60, 60]
