from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pandas as pd
import pytest
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.connection.config import get_connection_config
from analytics_toolkit.sql.dml.transfer.flow import (
    adaptive_input,
    attempt,
    finalize,
    staged_attempt,
)
from analytics_toolkit.sql.dml.transfer.runtime.models import (
    RowBatch,
    TransferConnectionRefs,
    TransferOptions,
    TransferStageState,
)
from analytics_toolkit.sql.execution.backend_options import backend_operation

from tests.sql._support.adaptive_transfer import install, install_native_snapshot


def test_guard_identity_handles_unordered_configuration_and_unrelated_connections():
    assert adaptive_input.configuration_digest_from_configs([{"features": {"a", "b"}}]) == (
        adaptive_input.configuration_digest_from_configs([{"features": {"b", "a"}}])
    )
    options = install_options()
    adaptive_input.install_configuration_guard(options, configured_only=True)
    assert not options.input_resolution
    with backend_operation() as operation:
        adaptive_input.install_configuration_guard(options)
        guard = next(iter(operation.resources["connection_configuration_guards"].values()))
        guard("unrelated", SimpleNamespace())
        guard("gp")
        with pytest.raises(ValueError, match="configuration differs"):
            guard("gp", replace(get_connection_config("gp"), database="other"))
        observed = {}
        operation.statuses[("other", "scope")] = {"alias": "other", "complete": False}
        adaptive_input._check_generation_scope(options, observed)
        assert not observed
        adaptive_input.remove_configuration_guard(options)
        assert not operation.resources["connection_configuration_guards"]
    adaptive_input.install_configuration_guard(options)
    adaptive_input._check_generation_scope(options, {})
    assert adaptive_input._availability_scope(options) == ()


def install_options():
    return TransferOptions("gp", "gp", "trino", "trino", "SELECT id", "sandbox.target")


def test_incomplete_identity_cannot_install_guard_for_a_different_configuration():
    options = install_options()
    options.input_resolution["configuration_digest"] = "different"
    with pytest.raises(ValueError, match="configuration differs"):
        adaptive_input.install_configuration_guard(options)


@pytest.mark.parametrize("corrupt_identity", [False, True])
def test_failed_snapshot_profile_reuses_only_the_original_native_checkpoint(
    monkeypatch, corrupt_identity
):
    scenario = install(monkeypatch, source_transfer_staging_schema="source_stage")
    events = install_native_snapshot(monkeypatch, scenario)
    scenario.fail_stream = True

    def read(*args, **kwargs):
        try:
            yield from scenario.read(*args, **kwargs)
        except ConnectionError:
            scenario.fail_stream = False
            if corrupt_identity:
                table, count, _digest, counts = scenario.options.input_resolution["native_snapshot"]
                scenario.options.input_resolution["native_snapshot"] = (
                    table,
                    count,
                    "other",
                    counts,
                )
            raise

    monkeypatch.setattr(adaptive_input, "iter_source_batches", read)
    if corrupt_identity:
        with pytest.raises(ValueError, match="native source checkpoint"):
            scenario.run()
        assert not scenario.loaded
        assert events == ["snapshot"]
    else:
        assert scenario.run() == 2
        assert events == ["snapshot", "drop_after_certification"]
        assert scenario.read_count == 2


def test_snapshot_count_mismatch_and_changed_columns_never_publish(monkeypatch):
    scenario = install(monkeypatch, source_transfer_staging_schema="source_stage", full_retry_cnt=1)
    install_native_snapshot(monkeypatch, scenario)
    monkeypatch.setattr(
        staged_attempt,
        "_materialize_snapshot",
        lambda *_a: ("source_stage.native_checkpoint", {0: 99}),
    )
    with pytest.raises(ValueError, match="count differs"):
        scenario.run()
    assert not scenario.loaded
    assert "create_target" not in scenario.events
    scenario = install(monkeypatch, full_retry_cnt=1)
    scenario.batches[scenario.options.source_sql] = [RowBatch(["changed"], [(1,)])]
    with pytest.raises(ValueError, match="Source columns changed"):
        scenario.run()
    assert not scenario.loaded


def test_existing_timestamp_implicit_precision_cannot_silently_narrow(monkeypatch):
    scenario = install(
        monkeypatch,
        native_type="DateTime64(9)",
        rows=[(pd.Timestamp("2024-01-01T00:00:00.000000001"),)],
    )
    scenario.existing_types = {"id": "TIMESTAMP(6)"}
    with pytest.raises(ValueError, match="authoritative timestamp precision"):
        scenario.run()
    assert "create_target" not in scenario.events


@pytest.mark.parametrize("change", ["configuration", "schema"])
def test_certified_retry_revalidates_frozen_identity_and_authority(monkeypatch, change):
    scenario = install(monkeypatch, table_schema={"id": "BIGINT"})
    scenario.fail_finalization_once = True
    original = attempt.run_certified_transfer_attempt

    def retry(options, context):
        if change == "configuration":
            spool = options.input_resolution["spool"]
            spool.identity = replace(spool.identity, configuration_digest="different")
        else:
            options.table_schema["id"] = "SMALLINT"
        return original(options, context)

    monkeypatch.setattr(attempt, "run_certified_transfer_attempt", retry)
    with pytest.raises(ValueError, match=r"(configuration|schema) differs"):
        scenario.run()
    assert len(scenario.loaded) == 1


@pytest.mark.parametrize("primary_failure", [False, True])
def test_certified_retry_cleanup_failure_preserves_the_primary_error(monkeypatch, primary_failure):
    scenario = install(monkeypatch)
    scenario.fail_finalization_once = True
    original = attempt.run_certified_transfer_attempt

    def retry(options, context):
        def fail_cleanup(**_kwargs):
            message = "checkpoint cleanup failed"
            raise OSError(message)

        monkeypatch.setattr(attempt, "cleanup_stage", fail_cleanup)
        if primary_failure:

            def fail_finalize(*_a, **_k):
                message = "target finalization failed"
                raise ValueError(message)

            monkeypatch.setattr(finalize, "finalize_loaded_stage", fail_finalize)
        return original(options, context)

    monkeypatch.setattr(attempt, "run_certified_transfer_attempt", retry)
    if primary_failure:
        with pytest.raises(ValueError, match="target finalization failed"):
            scenario.run()
    else:
        with pytest.raises(OSError, match="checkpoint cleanup failed"):
            scenario.run()


def test_optional_stream_close_protocol_is_not_required_for_complete_frozen_input(monkeypatch):
    scenario = install(monkeypatch)
    monkeypatch.setattr(
        adaptive_input,
        "iter_source_batches",
        lambda *_a, **_k: iter([RowBatch(["id"], [(1,), (2,)])]),
    )
    monkeypatch.setattr(attempt, "iter_source_batches", adaptive_input.iter_source_batches)
    assert scenario.run() == 2
    assert scenario.loaded == [[(1,), (2,)]]


def test_certified_replacement_survives_failure_to_release_native_source_checkpoint(monkeypatch):
    scenario = install(monkeypatch, source_transfer_staging_schema="source_stage")
    events = install_native_snapshot(monkeypatch, scenario)
    monkeypatch.setattr(staged_attempt, "get_sql_connection", attempt.get_sql_connection)
    original = adaptive_input.cleanup_stage_table
    attempts = []

    def drop(*args, **kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            message = "source checkpoint cleanup unavailable"
            raise ConnectionError(message)
        original(*args, **kwargs)

    monkeypatch.setattr(adaptive_input, "cleanup_stage_table", drop)
    assert scenario.run() == 2
    assert scenario.read_count == 1
    assert scenario.source_open_count == 2
    assert len(attempts) == 2
    assert events == ["snapshot", "drop_after_certification"]
    assert "native_snapshot" not in scenario.options.input_resolution


def test_adaptive_upsert_preserves_complete_writable_target_authority(monkeypatch):
    scenario = install(monkeypatch, write_mode="upsert", key_columns=["id"])
    scenario.existing_types = {"id": "BIGINT"}
    monkeypatch.setattr(
        get_backend_adapter("trino"),
        "get_writable_table_column_types",
        lambda *_a, **_k: {"id": "BIGINT"},
    )
    assert scenario.run() == 2
    assert scenario.schemas == [{"id": "BIGINT"}]
    assert scenario.loaded == [[(0,), (1,)]]


def test_unknown_source_schema_defers_resolution_without_allocating_input():
    opts = install_options()
    state = TransferStageState(False)
    assert not adaptive_input.resolve_adaptive_input(
        opts,
        TransferConnectionRefs(),
        state,
        [],
        adaptive_input.AdaptiveTransferContext(1, 1),
    )
    assert not opts.input_resolution
    assert state.stage_column_types is None


@pytest.mark.parametrize("estimate", [None, 0, 42])
def test_native_stream_estimates_keep_unknown_and_genuine_zero_distinct(estimate):
    opts = replace(install_options(), progress=True, estimate_total_rows=True)
    assert (
        adaptive_input.estimated_input_rows(opts, {"connection": object()}, lambda *_a: estimate)
        == estimate
    )
