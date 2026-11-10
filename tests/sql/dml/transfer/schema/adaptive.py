from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pandas as pd
from analytics_toolkit.general import set_connections_path

from tests.sql._support.adaptive_transfer import (
    AsyncSqlCancelled,
    RowBatch,
    TransferSlice,
    backend_operation,
    install,
    install_capped_keyed_replay,
    install_native_snapshot,
    pytest,
    require_streaming_fast_path,
    track_spools,
)


@pytest.mark.parametrize(
    ("rows", "expected_type", "expected_values"),
    [
        ([(0,), (1,)], "BIGINT", [(0,), (1,)]),
        ([(None,), (2**53 + 1,), (None,)], "BIGINT", [(None,), (2**53 + 1,), (None,)]),
        ([(2**64 - 1,)], "DECIMAL(20, 0)", [(Decimal(2**64 - 1),)]),
        ([(0,), (2**255 - 1,)], "VARCHAR", [("0",), (str(2**255 - 1),)]),
    ],
)
def test_transfer_profiles_every_selected_value_before_target_creation(
    monkeypatch: pytest.MonkeyPatch,
    rows: list[tuple[object, ...]],
    expected_type: str,
    expected_values: list[tuple[object, ...]],
) -> None:
    scenario = install(monkeypatch, rows=rows)

    result = scenario.run(metadata=True)

    assert result.rows == len(rows)
    assert scenario.schemas == [{"id": expected_type}]
    assert scenario.loaded == [expected_values]
    assert scenario.events.index("complete:SELECT id FROM source") < scenario.events.index(
        "create_target"
    )
    assert scenario.read_count == 1
    assert result.metadata.input_count == len(rows)
    assert result.metadata.type_resolutions[0]["certified_input_count"] == len(rows)
    assert "spool" not in scenario.options.input_resolution


def test_adaptive_transfer_decimal_preserves_coefficient_and_exponent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = [Decimal("1.2"), Decimal("0.0034"), Decimal("1E+5")]
    scenario = install(
        monkeypatch, native_type="Decimal256(40)", rows=[(value,) for value in values]
    )

    assert scenario.run() == 3

    assert scenario.schemas == [{"id": "DECIMAL(10, 4)"}]
    assert scenario.loaded == [[(value,) for value in values]]


def test_all_keyed_slices_contribute_to_one_schema_before_any_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slices = [
        TransferSlice(0, (1,), "", "SELECT first", None),
        TransferSlice(1, (2,), "", "SELECT last", None),
    ]
    scenario = install(monkeypatch, transfer_slices=slices)
    scenario.batches = {
        "SELECT first": [RowBatch(["id"], [(1,)])],
        "SELECT last": [RowBatch(["id"], [(2**255 - 1,)])],
    }

    result = scenario.run(metadata=True)

    assert result.rows == 2

    assert scenario.schemas == [{"id": "VARCHAR"}]
    assert scenario.loaded == [[("1",), (str(2**255 - 1),)]]
    assert scenario.events.index("complete:SELECT last") < scenario.events.index("create_target")
    assert scenario.read_count == 2


def test_certified_rows_survive_target_retry_without_mutable_source_requery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = install(monkeypatch, rows=[(2**64 - 1,)])
    scenario.fail_finalization_once = True

    assert scenario.run() == 1

    assert scenario.read_count == 1
    assert scenario.loaded == [[(Decimal(2**64 - 1),)], [(Decimal(2**64 - 1),)]]
    assert scenario.finalizations == 2


def test_certified_retry_does_not_need_the_original_source_to_remain_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = install(monkeypatch, rows=[(2**64 - 1,)])
    scenario.fail_finalization_once = True
    scenario.make_source_unavailable_on_retry = True

    result = scenario.run(metadata=True)

    assert result.rows == 1
    assert result.metadata.input_count == 1
    assert scenario.source_open_count == 1
    assert scenario.read_count == 1
    assert scenario.loaded == [[(Decimal(2**64 - 1),)], [(Decimal(2**64 - 1),)]]
    assert scenario.finalizations == 2


@pytest.mark.parametrize("existing", [False, True])
def test_authoritative_integer_schema_rejects_out_of_range_before_target_mutation(
    monkeypatch: pytest.MonkeyPatch,
    existing: bool,
) -> None:
    scenario = install(
        monkeypatch, rows=[(2**64 - 1,)], table_schema=None if existing else {"id": "BIGINT"}
    )
    if existing:
        scenario.existing_types = {"id": "BIGINT"}

    with pytest.raises(ValueError, match="authoritative integer"):
        scenario.run()

    assert "create_target" not in scenario.events
    assert scenario.loaded == []


def test_explicit_replacement_schema_authorizes_a_deliberate_native_widening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = install(
        monkeypatch,
        rows=[(2**64 - 1,)],
        table_schema={"id": "DECIMAL(20, 0)"},
        write_mode="replace",
    )
    scenario.existing_types = {"id": "BIGINT"}

    assert scenario.run() == 1

    assert scenario.schemas == [{"id": "DECIMAL(20, 0)"}]
    assert scenario.loaded == [[(Decimal(2**64 - 1),)]]
    assert scenario.finalizations == 1


def test_interrupted_profile_cannot_publish_or_replay_partial_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = install(monkeypatch, full_retry_cnt=1)
    scenario.fail_stream = True
    paths = track_spools(monkeypatch)
    with pytest.raises(ConnectionError, match="stream interrupted"):
        scenario.run()

    assert scenario.loaded == []
    assert "create_target" not in scenario.events
    assert paths
    assert all(not path.exists() for path in paths)
    assert "spool" not in scenario.options.input_resolution


def test_metadata_proven_native_domain_keeps_the_streaming_fast_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = install(monkeypatch, native_type="Int64", rows=[(2**53 + 1,)])
    require_streaming_fast_path(monkeypatch)

    assert scenario.run() == 1

    assert scenario.read_count == 1
    assert scenario.loaded == [[(2**53 + 1,)]]
    assert scenario.events.index("create_target") < scenario.events.index(
        "read:SELECT id FROM source"
    )


def test_configured_native_snapshot_is_replaced_only_after_spool_certification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = install(
        monkeypatch, source_transfer_staging_schema="source_stage", rows=[(1,), (2**255 - 1,)]
    )
    events = install_native_snapshot(monkeypatch, scenario)

    assert scenario.run() == 2

    assert events == ["snapshot", "drop_after_certification"]
    assert scenario.read_count == 1
    assert "read:SELECT id FROM source" not in scenario.events
    assert scenario.loaded == [[("1",), (str(2**255 - 1),)]]


def test_changed_availability_scope_invalidates_the_whole_input_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = install(monkeypatch, full_retry_cnt=1)
    scenario.change_scope = True
    paths = track_spools(monkeypatch)

    with pytest.raises(ValueError, match="availability scope changed"):
        scenario.run()

    assert "create_target" not in scenario.events
    assert scenario.loaded == []
    assert paths
    assert all(not path.exists() for path in paths)


def test_changed_configuration_invalidates_an_uncertified_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = install(monkeypatch, full_retry_cnt=1)
    scenario.change_configuration = True
    paths = track_spools(monkeypatch)

    with pytest.raises(ValueError, match="configuration changed"):
        scenario.run()

    assert scenario.loaded == []
    assert "create_target" not in scenario.events
    assert paths
    assert all(not path.exists() for path in paths)


@pytest.mark.parametrize(
    ("field", "value"),
    [("database", "changed_namespace"), ("password", " changed credential ")],
    ids=["namespace", "credential"],
)
def test_changed_configuration_cannot_replay_certified_rows_to_a_new_namespace(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    scenario = install(monkeypatch)
    scenario.fail_finalization_once = True
    scenario.change_configuration_on_retry = True
    scenario.configuration_field = field
    scenario.configuration_value = value

    with pytest.raises(ValueError, match="configuration differs from certified"):
        scenario.run()

    assert scenario.read_count == 1
    assert len(scenario.loaded) == 1
    assert scenario.events.count("create_target") == 1
    assert scenario.finalizations == 1


def test_adaptive_keyed_replay_respects_cap_one_and_uses_certified_estimate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = install(monkeypatch, rows=[(0,), (1,)])
    connections = install_capped_keyed_replay(monkeypatch, scenario)

    result = scenario.run(metadata=True)

    assert result.rows == 2
    assert result.metadata.source_stage_count == 1
    assert result.metadata.live_source_stage_limit == 1
    assert result.metadata.effective_read_concurrency == 1
    assert result.metadata.effective_write_concurrency == 1

    assert scenario.read_count == 1
    assert scenario.loaded == [[(0,)], [(1,)]]
    assert scenario.schemas == [{"id": "BIGINT"}, {"id": "BIGINT"}]
    assert connections.maximum == {"ch": 1, "trino": 1}
    assert connections.active == {"ch": 0, "trino": 0}
    assert scenario.finalizations == 1
    assert "target_connection_runner" not in scenario.options.input_resolution


@pytest.mark.parametrize("reconnect", [False, True])
def test_configuration_change_after_first_batch_cannot_rebind_a_target_or_leak_guards(
    monkeypatch: pytest.MonkeyPatch,
    reconnect: bool,
) -> None:
    scenario = install(monkeypatch, rows=[(0,), (1,)])
    scenario.change_configuration_after_insert = True
    scenario.reconnect_after_configuration_change = reconnect
    connections = install_capped_keyed_replay(monkeypatch, scenario)

    with backend_operation() as operation:
        with pytest.raises(ValueError, match="configuration differs from certified"):
            scenario.run()
        assert not operation.resources.get("connection_configuration_guards")

    assert scenario.read_count == 1
    assert scenario.loaded == [[(0,)]]
    assert scenario.finalizations == 0
    assert scenario.unexpected_driver_opens == []
    assert connections.active == {"ch": 0, "trino": 0}


def test_cancelled_profiling_releases_its_spool_and_configuration_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = install(monkeypatch)
    scenario.cancel_stream = True
    paths = track_spools(monkeypatch)

    with backend_operation() as operation:
        with pytest.raises(AsyncSqlCancelled):
            scenario.run()
        assert not operation.resources.get("connection_configuration_guards")

    assert "create_target" not in scenario.events
    assert scenario.loaded == []
    assert paths
    assert all(not path.exists() for path in paths)


def test_transfer_dry_run_declares_pending_resolution_without_scanning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = install(monkeypatch)

    plan = scenario.run(dry_run=True)

    assert scenario.read_count == 0
    assert scenario.events == []
    assert plan.options["type_resolution_pending"] is True
    assert "BIGINT" in plan.options["type_resolution"]["native_candidates"]
    assert plan.options["type_resolution"]["dry_run_profiles_input"] is False
    assert any(statement.phase == "resolve_types" for statement in plan.statements)


@pytest.mark.parametrize(("stage_scale", "allowed"), [(9, False), (3, True)])
def test_existing_timestamp_validates_after_only_the_requested_explicit_conversion(
    monkeypatch: pytest.MonkeyPatch,
    stage_scale: int,
    allowed: bool,
) -> None:
    path = Path(".connections")
    configs = json.loads(path.read_text())
    configs["ch_target"] = {**configs["ch"], "database": "target"}
    path.write_text(json.dumps(configs))
    set_connections_path(path)
    value = pd.Timestamp("2024-01-01T00:00:00.123456789Z")
    scenario = install(
        monkeypatch,
        native_type="DateTime64(9, 'UTC')",
        rows=[(value,)],
        to_db_key="ch_target",
        to_db_backend="ch",
        target_table="target.events",
        table_schema={"id": f"DateTime64({stage_scale}, 'UTC')"},
        write_mode="append",
        replace_target_table=False,
    )
    scenario.existing_types = {"id": "DateTime64(6, 'UTC')"}
    if allowed:
        assert scenario.run() == 1
        assert scenario.loaded == [[(pd.Timestamp("2024-01-01T00:00:00.123Z"),)]]
    else:
        with pytest.raises(ValueError, match="precision"):
            scenario.run()
        assert "create_target" not in scenario.events
        assert not scenario.loaded
