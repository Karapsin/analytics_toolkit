"""Selected client codecs are certified before public transfer publication."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest
from analytics_toolkit.sql.dml.transfer.flow import stage
from analytics_toolkit.sql.dml.transfer.flow.client_validation import (
    prepare_client_write_validation,
)
from analytics_toolkit.sql.dml.transfer.runtime.models import (
    TransferConnectionRefs,
    TransferStageState,
)

from tests.sql._support.adaptive_transfer import RowBatch, TransferSlice
from tests.sql._support.ch_native_aliases import http_factory_available
from tests.sql._support.ch_transfer_client_validation import (
    ClientConnection,
    insert_client_rows,
    install_client_transfer,
)
from tests.sql.backends.ch._support.network_transport import encode_normalized


def test_ch_client_validation_http_object_factory_precedes_source_and_live_mutation(
    monkeypatch, write_sql_connections
):
    native = "Object('json')"
    rows = [({"a": 1},), ({"a": 2},)]
    seed = [({"seed": 1},)]
    scenario, target = install_client_transfer(
        monkeypatch,
        write_sql_connections,
        source_types={"id": native},
        target_types={"id": native},
        rows=rows,
        transport="http",
        seed=seed,
        write_mode="truncate_insert",
    )

    # Capability follows the installed factory rather than an SDK version guess.
    if not http_factory_available(native):
        with pytest.raises(ValueError, match="selected ClickHouse client"):
            scenario.run()
        assert scenario.read_count == 1
        assert scenario.events == [f"read:{scenario.options.source_sql}"]
        assert scenario.loaded == target.emitted_batches == target.wire_columns == []
        assert "create_target" not in scenario.events
        assert scenario.finalizations == 0
        assert target.live_rows == seed
    else:
        result = scenario.run(metadata=True)
        assert result.rows == result.metadata.input_count == len(rows)
        assert target.live_rows == rows
        assert scenario.events.index(f"complete:{scenario.options.source_sql}") < (
            scenario.events.index("create_target")
        )


@pytest.mark.parametrize(
    ("native", "values"),
    [
        ("Point", [(1.25, 2.5), (-3.5, 4.75)]),
        ("SimpleAggregateFunction(sum, UInt64)", [7, 2**64 - 1]),
    ],
    ids=["point", "simple-aggregate"],
)
def test_ch_client_validation_declared_structural_domain_stays_lazy(
    monkeypatch, write_sql_connections, native, values
):
    rows = [(value,) for value in values]
    expected_wire = encode_normalized(native, deepcopy(values), "native")
    scenario, target = install_client_transfer(
        monkeypatch,
        write_sql_connections,
        source_types={"id": native},
        target_types={"id": native},
        rows=rows,
    )

    result = scenario.run(metadata=True)

    assert result.rows == len(rows)
    assert result.metadata.input_count is None
    assert result.metadata.type_resolutions == []
    assert scenario.events.index("create_target") < scenario.events.index(
        f"read:{scenario.options.source_sql}"
    )
    assert scenario.source_open_count == scenario.read_count == 1
    assert scenario.loaded == target.emitted_batches == [rows]
    assert target.wire_columns == [("id", native, expected_wire)]
    capability = target.validations[0]["capability"]
    assert capability.validation_basis == "declared-source-domain"
    assert capability.certified_input_count is None
    assert target.validations[0]["spool_digest"] is None


@pytest.mark.parametrize("existing", [False, True], ids=["new", "existing"])
def test_ch_client_validation_late_structural_range_error_precedes_target_mutation(
    monkeypatch, write_sql_connections, existing
):
    native = "SimpleAggregateFunction(sum, UInt8)"
    seed = [(5,)] if existing else None
    scenario, target = install_client_transfer(
        monkeypatch,
        write_sql_connections,
        source_types={"id": "UInt64"},
        target_types={"id": native},
        rows=[(7,), (256,)],
        seed=seed,
        write_mode="truncate_insert",
    )

    with pytest.raises(ValueError, match=r"authoritative|encoder|Selected input"):
        scenario.run()

    assert scenario.events == [
        f"read:{scenario.options.source_sql}",
        f"complete:{scenario.options.source_sql}",
    ]
    assert scenario.read_count == 1
    assert scenario.loaded == target.emitted_batches == []
    assert scenario.finalizations == 0
    assert target.live_rows == (seed or [])


def test_ch_client_validation_late_object_failure_scans_all_keys_before_publication(
    monkeypatch, write_sql_connections
):
    slices = [
        TransferSlice(0, (1,), "", "SELECT first", None),
        TransferSlice(1, (2,), "", "SELECT last", None),
    ]
    seed = [({"seed": 1},)]
    scenario, target = install_client_transfer(
        monkeypatch,
        write_sql_connections,
        source_types={"id": "String"},
        target_types={"id": "Object('json')"},
        rows=[('{"a":1}',), ("[2]",)],
        seed=seed,
        write_mode="truncate_insert",
        transfer_slices=slices,
    )
    scenario.batches = {
        "SELECT first": [RowBatch(["id"], [('{"a":1}',)])],
        "SELECT last": [RowBatch(["id"], [("[2]",)])],
    }

    with pytest.raises(ValueError, match=r"Object|authoritative|encoder|Selected input"):
        scenario.run()

    assert scenario.read_count == 2
    assert scenario.events == [
        "read:SELECT first",
        "complete:SELECT first",
        "read:SELECT last",
        "complete:SELECT last",
    ]
    assert scenario.loaded == target.emitted_batches == target.wire_columns == []
    assert scenario.finalizations == 0
    assert target.live_rows == seed


def test_ch_client_validation_object_metadata_still_requires_complete_native_input(
    monkeypatch, write_sql_connections
):
    native = "Object('json')"
    values = [{"a": 1}, {"a": 2, "b": "value"}]
    rows = [(value,) for value in values]
    scenario, target = install_client_transfer(
        monkeypatch,
        write_sql_connections,
        source_types={"id": native},
        target_types={"id": native},
        rows=rows,
    )

    result = scenario.run(metadata=True)

    assert result.rows == result.metadata.input_count == len(rows)
    assert scenario.events.index(f"complete:{scenario.options.source_sql}") < (
        scenario.events.index("create_target")
    )
    expected_text = ['{"a": 1}', '{"a": 2, "b": "value"}']
    assert target.emitted_batches == [[(value,) for value in expected_text]]
    assert [json.loads(row[0]) for row in target.emitted_batches[0]] == values
    assert [
        json.loads(row[0]) if isinstance(row[0], str) else row[0] for row in scenario.loaded[0]
    ] == values
    assert target.wire_columns == [
        ("id", native, encode_normalized(native, expected_text, "native"))
    ]
    capability = target.validations[0]["capability"]
    assert capability.validation_basis == "encoded-input"
    assert capability.opaque_columns == ("id",)
    assert capability.certified_input_count == len(rows)


def test_ch_client_validation_keyed_retry_reuses_complete_encoder_certificate(
    monkeypatch, write_sql_connections
):
    native = "Point"
    slices = [
        TransferSlice(0, (1,), "", "SELECT first", None),
        TransferSlice(1, (2,), "", "SELECT last", None),
    ]
    rows = [((1.25, 2.5), 0), ((-3.5, 4.75), 1)]
    schema = {"id": native, "amount": "Int64"}
    scenario, target = install_client_transfer(
        monkeypatch,
        write_sql_connections,
        source_types={"id": native, "amount": "Int256"},
        target_types=schema,
        rows=rows,
        transfer_slices=slices,
        write_mode="replace",
    )
    scenario.batches = {
        "SELECT first": [RowBatch(["id", "amount"], [rows[0]])],
        "SELECT last": [RowBatch(["id", "amount"], [rows[1]])],
    }
    scenario.fail_finalization_once = True
    scenario.make_source_unavailable_on_retry = True

    result = scenario.run(metadata=True)

    assert result.rows == result.metadata.input_count == len(rows)
    assert scenario.read_count == len(slices)
    assert scenario.source_open_count == 1
    assert scenario.finalizations == 2
    assert scenario.loaded == target.emitted_batches == [rows, rows]
    assert target.live_rows == rows
    assert scenario.events.index("complete:SELECT last") < scenario.events.index("create_target")
    first, second = target.validations
    assert first["certificate"] is second["certificate"]
    assert first["capability"].validation_basis == "encoded-input"
    assert first["capability"].certified_input_count == len(rows)
    assert first["spool_digest"] == second["spool_digest"]
    assert first["spool_digest"] is not None


@pytest.mark.parametrize(
    ("physical", "compatible"),
    [("Tuple(String, String)", False), ("Tuple(Float64, Float64)", True)],
    ids=["incompatible-server-sample", "canonical-server-sample"],
)
def test_ch_client_validation_real_stage_hook_checks_acknowledged_native_sample(
    monkeypatch, write_sql_connections, physical, compatible
):
    schema = {"id": "Point"}
    rows = [((1.25, 2.5),)]
    seed = [((99.0, 99.0),)]
    scenario, target = install_client_transfer(
        monkeypatch,
        write_sql_connections,
        source_types=schema,
        target_types=schema,
        rows=rows,
        seed=seed,
        physical_schema={"id": physical},
    )
    connection = ClientConnection(target)
    assert not prepare_client_write_validation(
        scenario.options,
        connection,
        scenario.source_schema,
        schema,
        authoritative=True,
    )
    state = TransferStageState(
        target_exists=True,
        source_columns=["id"],
        source_column_types=schema,
        stage_column_types=schema,
        input_types_resolved=True,
    )
    creations = []

    def acknowledged_create(**kwargs):
        assert kwargs["column_types"] == schema
        name = "sandbox.private_payload"
        kwargs["on_stage_candidate"](name)
        kwargs["on_resource_created"](name)
        creations.append(name)
        return name

    monkeypatch.setattr(stage, "create_stage_table", acknowledged_create)
    refs = TransferConnectionRefs(target={"connection": connection})
    batch = RowBatch(["id"], rows)

    if compatible:
        stage.initialize_stage_for_first_batch(scenario.options, refs, state, batch)
        insert_client_rows(target, state.stage_column_types, rows)
        assert target.emitted_batches == [rows]
        assert target.wire_columns == [
            ("id", physical, encode_normalized(physical, [rows[0][0]], "native"))
        ]
        certificate = scenario.options.input_resolution["client_write_certificate"][1]
        assert certificate.column_types == (("id", physical),)
        assert certificate.validation_basis == "declared-source-domain"
        assert certificate.certified_input_count is None
    else:
        with pytest.raises(ValueError, match="stage differs"):
            stage.initialize_stage_for_first_batch(scenario.options, refs, state, batch)
        assert target.emitted_batches == target.wire_columns == []

    assert creations == ["sandbox.private_payload"]
    assert target.metadata_queries == ["DESCRIBE TABLE sandbox.private_payload"]
    assert state.owned_stage_resources == [("sandbox.private_payload", "sandbox.private_payload")]
    assert scenario.read_count == scenario.finalizations == 0
    assert target.live_rows == seed


def test_ch_client_validation_empty_http_object_keeps_sql_only_creation(
    monkeypatch, write_sql_connections
):
    native = "Object('json')"
    scenario, target = install_client_transfer(
        monkeypatch,
        write_sql_connections,
        source_types={"id": native},
        target_types={"id": native},
        rows=[],
        transport="http",
    )
    # adaptive_transfer.install deliberately supplies default rows for [].
    # This source has no selected rows or source batches at all.
    scenario.batches[scenario.options.source_sql] = []

    result = scenario.run(metadata=True)

    assert result.rows == result.metadata.source_rows == result.metadata.input_count == 0
    assert scenario.source_open_count == scenario.read_count == 1
    assert scenario.events.index(f"complete:{scenario.options.source_sql}") < (
        scenario.events.index("create_target")
    )
    assert scenario.schemas == [{"id": native}]
    assert scenario.loaded == [[]]
    assert target.emitted_batches == target.wire_columns == target.validations == []
    assert target.live_rows == []
    assert "spool" not in scenario.options.input_resolution
