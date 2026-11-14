"""Native checkpoint identity and ownership guards complement real-driver tests."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from analytics_toolkit.sql.backends.gp.native_assignment import NativeAssignmentCertificate
from analytics_toolkit.sql.backends.models import SourceColumn
from analytics_toolkit.sql.core.replay_spool import FrozenInputIdentity
from analytics_toolkit.sql.core.type_resolution import ColumnProfile, ResolvedColumn
from analytics_toolkit.sql.dml.transfer.flow import adaptive_input
from analytics_toolkit.sql.dml.transfer.flow import native_assignment as native
from analytics_toolkit.sql.dml.transfer.runtime.models import (
    TransferConnectionRefs,
    TransferStageState,
)


def checkpoint(*, final=True):
    identity = FrozenInputIdentity(source_key="source", query_digest="selected-once")
    schema = (("payload", "CIDR"),)
    certificate = NativeAssignmentCertificate(
        "sandbox.events__stage__owned", schema, 2, (), (), identity
    )
    cached = native._RetainedAssignment(
        certificate,
        ((certificate.stage_table, certificate.stage_table),),
        schema,
        final=final,
    )
    options = SimpleNamespace(
        to_db_backend="gp",
        to_db_key="gp",
        query_label=None,
        replace_target_table=True,
        input_resolution={
            "spool": SimpleNamespace(identity=identity),
            "native_assignment_pending": True,
            "native_assignment_checkpoint": cached,
        },
    )
    state = TransferStageState(
        target_exists=True,
        source_columns=["payload"],
        stage_column_types=dict(schema),
        insert_column_types=dict(schema),
    )
    return options, state, cached


def fail_primary(error):
    raise error


def test_native_checkpoint_capability_defaults_strict_for_other_query_consumers():
    assert not adaptive_input.AdaptiveTransferContext(1, 1).allow_pending_native_assignment


def test_native_checkpoint_same_alias_identical_native_domain_keeps_metadata_fast_path(monkeypatch):
    options = SimpleNamespace(
        from_db_key="gp",
        to_db_key="gp",
        from_db_backend="gp",
        to_db_backend="gp",
        table_schema={"payload": "CIDR"},
        input_resolution={},
    )
    state = TransferStageState(target_exists=False)
    monkeypatch.setattr(
        adaptive_input, "ReplaySpool", lambda *_args, **_kwargs: pytest.fail("unneeded scan")
    )

    assert not adaptive_input.resolve_adaptive_input(
        options,
        TransferConnectionRefs(target={"connection": object()}),
        state,
        [SourceColumn("payload", "CIDR")],
        adaptive_input.AdaptiveTransferContext(1, 1, allow_pending_native_assignment=True),
    )

    assert state.stage_column_types == {"payload": "CIDR"}
    assert "spool" not in options.input_resolution
    assert not options.input_resolution.get("used")
    assert not options.input_resolution.get("native_assignment_pending")


@pytest.mark.parametrize("new_type", ["MACADDR", "INET"])
def test_native_checkpoint_rejects_changed_target_schema_before_reading_stage(new_type):
    options, state, _cached = checkpoint()
    state.insert_column_types = {"payload": new_type}

    with pytest.raises(ValueError, match="Destination schema differs"):
        native.restore_assignment_checkpoint(options, state, lambda *_args: pytest.fail("SQL"))


def test_native_checkpoint_rejects_different_selected_query_before_reading_stage():
    options, state, cached = checkpoint()
    options.input_resolution["spool"].identity = replace(
        cached.certificate.input_identity, query_digest="different-selection"
    )

    with pytest.raises(ValueError, match="Selected input differs"):
        native.restore_assignment_checkpoint(options, state, lambda *_args: pytest.fail("SQL"))


def test_native_checkpoint_requires_authoritative_types_before_restore():
    options, state, _cached = checkpoint()
    state.insert_column_types = None
    state.stage_column_types = None

    with pytest.raises(ValueError, match="authoritative destination schema"):
        native.restore_assignment_checkpoint(options, state, lambda *_args: pytest.fail("SQL"))


@pytest.mark.parametrize("missing", ["table", "schema"])
def test_native_checkpoint_parameter_binding_requires_physical_stage_and_schema(
    monkeypatch, missing
):
    options, state, cached = checkpoint()
    state.stage_table = cached.certificate.stage_table
    if missing == "table":
        state.stage_table = None
    else:
        state.stage_column_types = None
    monkeypatch.setattr(
        native,
        "assignment_parameter_types",
        lambda *_args, **_kwargs: pytest.fail("Unprepared input inspected native catalog"),
    )

    with pytest.raises(ValueError, match="require an acknowledged typed stage"):
        native.native_insert_parameter_types(options, state, object())

    assert "native_assignment_parameter_types" not in options.input_resolution
    assert state.owned_stage_resources == []


def test_native_checkpoint_empty_input_leaves_publication_to_existing_empty_policy():
    options, state, _cached = checkpoint()
    options.input_resolution.pop("native_assignment_checkpoint")

    assert (
        native.certify_loaded_assignment(options, state, 0, lambda *_args: pytest.fail("SQL"))
        is None
    )

    assert "native_assignment_checkpoint" not in options.input_resolution
    assert "native_assignment_certificate" not in options.input_resolution
    assert state.stage_table is None
    assert state.owned_stage_resources == []


@pytest.mark.parametrize("missing", ["table", "schema"])
def test_native_checkpoint_cannot_certify_missing_physical_stage_or_schema(missing):
    options, state, _cached = checkpoint()
    options.input_resolution.pop("native_assignment_checkpoint")
    state.stage_table = "sandbox.unowned"
    if missing == "table":
        state.stage_table = None
    else:
        state.stage_column_types = None

    with pytest.raises(ValueError, match="completely populated source stage"):
        native.certify_loaded_assignment(options, state, 2, lambda *_args: pytest.fail("SQL"))


def test_native_checkpoint_rejects_unacknowledged_candidate_before_inspecting_it():
    options, state, _cached = checkpoint()
    options.input_resolution.pop("native_assignment_checkpoint")
    state.stage_table = "sandbox.colliding_candidate"
    state.stage_table_candidates = [state.stage_table]

    with pytest.raises(ValueError, match="no acknowledged owned resource"):
        native.certify_loaded_assignment(options, state, 2, lambda *_args: pytest.fail("SQL"))


def test_native_checkpoint_deferral_retains_known_final_integer_range_validation():
    options, state, _cached = checkpoint()
    options.input_resolution.pop("native_assignment_pending")
    state.insert_column_types = {"payload": "CIDR", "count": "SMALLINT"}
    profiles = [ColumnProfile(), ColumnProfile()]
    profiles[0].add("192.0.2.0/24")
    profiles[1].add(40000)
    stages = [
        ResolvedColumn("CIDR", target_backend="gp", native_assignment_required=True),
        ResolvedColumn("BIGINT", target_backend="gp"),
    ]

    with pytest.raises(ValueError, match="authoritative integer"):
        native.validate_pending_final_conversions(
            options,
            state,
            [SourceColumn("payload", "String"), SourceColumn("count", "Int64")],
            stages,
            profiles,
            SimpleNamespace(iter_rows=lambda: iter([("192.0.2.0/24", 40000)])),
        )

    assert "native_assignment_pending" not in options.input_resolution


def test_native_checkpoint_cleanup_preserves_structural_recovery_artifacts(monkeypatch, capsys):
    options, _state, cached = checkpoint()
    monkeypatch.setattr(native, "preserve_recovery_artifacts", lambda: True)
    monkeypatch.setattr(
        native, "run_with_fresh_connection", lambda *_args, **_kwargs: pytest.fail("SQL")
    )

    native.close_assignment_checkpoint(options)

    assert cached.certificate.stage_table in capsys.readouterr().out


def test_native_checkpoint_cleanup_does_not_follow_retargeted_connection_alias(monkeypatch, capsys):
    options, _state, cached = checkpoint()

    def reject(_options):
        message = "Connection configuration differs from certified transfer input."
        raise ValueError(message)

    monkeypatch.setattr(adaptive_input, "require_frozen_configuration", reject)
    monkeypatch.setattr(
        native, "run_with_fresh_connection", lambda *_args, **_kwargs: pytest.fail("retargeted SQL")
    )
    try:
        fail_primary(RuntimeError("original assignment failure"))
    except RuntimeError:
        native.close_assignment_checkpoint(options)

    assert cached.certificate.stage_table in capsys.readouterr().out


@pytest.mark.parametrize("primary", [False, True])
def test_native_checkpoint_terminal_cleanup_reports_failure_without_masking_primary(
    monkeypatch, capsys, primary
):
    options, _state, cached = checkpoint()
    monkeypatch.setattr(adaptive_input, "require_frozen_configuration", lambda _options: None)

    def fail(*_args, **_kwargs):
        message = "checkpoint cleanup failed"
        raise RuntimeError(message)

    monkeypatch.setattr(native, "run_with_fresh_connection", fail)
    if primary:
        try:
            fail_primary(ValueError("primary operation failed"))
        except ValueError:
            native.close_assignment_checkpoint(options)
        assert cached.certificate.stage_table in capsys.readouterr().out
    else:
        with pytest.raises(RuntimeError, match="checkpoint cleanup failed"):
            native.close_assignment_checkpoint(options)


def test_native_checkpoint_metadata_reports_final_type_without_rewriting_explicit_stage_type():
    options, _state, cached = checkpoint()
    options.input_resolution["native_assignment_certificate"] = cached.certificate
    metadata = SimpleNamespace(
        type_resolutions=[
            {
                "column": "payload",
                "source_type": "String",
                "destination_type": "TEXT",
                "certified_input_count": 0,
            }
        ]
    )

    native.annotate_assignment_metadata(options, metadata)

    assert metadata.type_resolutions == [
        {
            "column": "payload",
            "source_type": "String",
            "destination_type": "TEXT",
            "native_assignment_destination_type": "CIDR",
            "native_assignment_validation": "certified",
            "native_assignment_required": False,
            "reason": "native_assignment_checkpoint",
            "certified_input_count": 2,
        }
    ]


def test_native_checkpoint_metadata_keeps_locally_certified_columns_and_unknown_defaults():
    options, _state, cached = checkpoint()
    certificate = replace(
        cached.certificate, column_types=(("payload", "CIDR"), ("count", "BIGINT"))
    )
    options.input_resolution["native_assignment_certificate"] = certificate
    metadata = SimpleNamespace(
        type_resolutions=[
            {
                "column": "payload",
                "destination_type": "CIDR",
                "native_assignment_required": True,
                "certified_input_count": 0,
            },
            {
                "column": "count",
                "destination_type": "BIGINT",
                "reason": "authoritative",
                "certified_input_count": 2,
            },
        ]
    )

    native.annotate_assignment_metadata(options, metadata)

    address, count = metadata.type_resolutions
    assert address["native_assignment_required"] is False
    assert address["certified_input_count"] == 2
    assert count["reason"] == "authoritative"
    assert "native_assignment_required" not in count
    assert count["native_assignment_validation"] == "certified"
    options.input_resolution.pop("native_assignment_certificate")
    empty = SimpleNamespace(type_resolutions=[])
    native.annotate_assignment_metadata(options, empty)
    assert empty.type_resolutions == []
