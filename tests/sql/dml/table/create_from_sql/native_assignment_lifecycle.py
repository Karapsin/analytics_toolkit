"""Query checkpoints retain input identity and clean only acknowledged resources."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from analytics_toolkit.sql.backends.gp.native_assignment import (
    NativeAssignmentCertificate,
    NativeAssignmentError,
)
from analytics_toolkit.sql.backends.models import SourceColumn
from analytics_toolkit.sql.core.replay_spool import FrozenInputIdentity
from analytics_toolkit.sql.dml import query_input
from analytics_toolkit.sql.dml import query_native_assignment as native
from analytics_toolkit.sql.dml.transfer.flow import attempt
from analytics_toolkit.sql.dml.transfer.runtime.models import TransferOptions
from analytics_toolkit.sql.execution.plans import SqlOperationMetadata


def checkpoint(monkeypatch):
    options = TransferOptions(
        from_db_key="gp_sandbox",
        from_db_backend="gp",
        to_db_key="gp",
        to_db_backend="gp",
        source_sql="SELECT payload FROM sandbox.source_rows",
        target_table="sandbox.events",
        canonical_destination_identity="sandbox.events",
        input_resolution={"configuration_digest": "frozen-endpoints"},
    )
    monkeypatch.setattr(native, "_require_configuration", lambda _options: None)
    schema = {"payload": "CIDR"}
    owned = native._owned_input(options, schema)
    stage = "sandbox.events__stage__owned"
    owned.state.owned_stage_resources.append((stage, stage))
    owned.state.stage_table_candidates.append(stage)
    owned.certificate = NativeAssignmentCertificate(
        stage, tuple(schema.items()), 2, (), (), owned.identity
    )
    return options, schema, owned


@pytest.mark.parametrize("changed", ["query", "namespace", "config", "schema", "timezone"])
def test_native_query_checkpoint_rejects_changed_binding_before_sql(monkeypatch, changed):
    options, schema, _owned = checkpoint(monkeypatch)
    if changed == "query":
        options = replace(options, source_sql="SELECT payload FROM sandbox.different")
    elif changed == "namespace":
        options = replace(options, canonical_destination_identity="other.events")
    elif changed == "config":
        options.input_resolution["configuration_digest"] = "different-endpoints"
    elif changed == "timezone":
        options.input_resolution["destination_timezone"] = "UTC"
    else:
        schema = {"payload": "MACADDR"}
    monkeypatch.setattr(native, "_certify", lambda *_args: pytest.fail("SQL after identity drift"))

    with pytest.raises(ValueError, match="Query or destination schema differs"):
        native.require_query_assignment_certificate(options, schema, {"connection": object()})


@pytest.mark.parametrize("changed", ["owner", "certificate_identity", "certificate_schema"])
def test_native_query_replay_requires_owned_matching_certificate(monkeypatch, changed):
    options, schema, owned = checkpoint(monkeypatch)
    if changed == "owner":
        owned.state.owned_stage_resources.clear()
    elif changed == "certificate_identity":
        owned.certificate = replace(
            owned.certificate, input_identity=FrozenInputIdentity(query_digest="different")
        )
    else:
        owned.certificate = replace(owned.certificate, column_types=(("payload", "INET"),))
    monkeypatch.setattr(native, "_certify", lambda *_args: pytest.fail("uncertified SQL"))

    with pytest.raises(ValueError, match=r"acknowledged owned|certificate differs"):
        native.require_query_assignment_certificate(options, schema, {"connection": object()})


def test_native_query_python_certificate_binds_actual_frozen_spool_identity(monkeypatch):
    options, _schema, owned = checkpoint(monkeypatch)
    identity = FrozenInputIdentity(
        source_key="ch", query_digest="once", source_timezone="UTC", destination_timezone="UTC"
    )
    options.input_resolution["spool"] = SimpleNamespace(identity=identity)
    assert native._identity(options) is identity
    assert native._identity(options) != owned.identity


@pytest.mark.parametrize(
    ("generation", "recoverable"),
    [
        ("unacknowledged", False),
        ("source_acked", True),
        ("complete_spool", True),
        ("incomplete_spool", False),
        ("no_generation", False),
    ],
)
def test_native_query_incomplete_generation_cannot_skip_source_script_setup(
    monkeypatch, generation, recoverable
):
    options, schema, owned = checkpoint(monkeypatch)
    if generation == "source_acked":
        owned.source_stage = "sandbox.owned_source"
    elif "spool" in generation:
        options.input_resolution["spool"] = SimpleNamespace(
            certified=generation == "complete_spool"
        )
    elif generation == "no_generation":
        options.input_resolution.pop("query_native_assignment")
    prepared = query_input.PreparedQueryInput(
        options, owned.state, schema, ["payload"], frozen=True
    )
    cache = {"prepared_query_input": prepared}
    failure = ConnectionError("transient pre-publication failure")

    def fail(*_args):
        raise failure

    monkeypatch.setattr(query_input, "prepare_native_sql_assignment", fail)
    monkeypatch.setattr(query_input, "prepare_python_query_assignment", fail)
    with pytest.raises(ConnectionError) as caught:
        query_input._prepare_assignment(
            prepared, {"connection": object()}, {"connection": object()}, cache
        )
    assert caught.value is failure
    assert (cache.get("prepared_query_input") is prepared) is recoverable


@pytest.mark.parametrize("generation", ["missing", "uncertified"])
def test_native_query_python_incomplete_input_cannot_load_or_publish(monkeypatch, generation):
    options, schema, owned = checkpoint(monkeypatch)
    owned.certificate = None
    if generation == "uncertified":
        options.input_resolution["spool"] = SimpleNamespace(
            identity=owned.identity, certified=False
        )
    prepared = query_input.PreparedQueryInput(
        options, owned.state, schema, ["payload"], frozen=True
    )
    monkeypatch.setattr(
        attempt, "load_stage_batches", lambda *_args: pytest.fail("read incomplete input")
    )
    monkeypatch.setattr(
        native, "_create_typed_stage", lambda *_args: pytest.fail("stage incomplete input")
    )
    monkeypatch.setattr(native, "_certify", lambda *_args: pytest.fail("certify incomplete input"))
    target_ref = {"connection": object()}

    with pytest.raises(ValueError, match="complete certified input checkpoint"):
        native.prepare_python_query_assignment(prepared, {"connection": object()}, target_ref)
    with pytest.raises(ValueError, match="certified owned checkpoint before replay"):
        query_input.replay_query_input(prepared, target_ref, SqlOperationMetadata())

    assert owned.certificate is None
    assert "target_connection_runner" not in options.input_resolution


def test_native_query_missing_native_metadata_cannot_certify_a_typed_snapshot(monkeypatch):
    options, schema, owned = checkpoint(monkeypatch)
    owned.certificate = None
    owned.source_stage = "sandbox.events__stage__source"
    calls = []
    adapter = SimpleNamespace(
        column_list_sql=lambda _columns: '"payload"',
        count_table_rows=lambda *_args, **_kwargs: calls.append("count"),
    )
    monkeypatch.setattr(native, "get_backend_adapter", lambda _backend: adapter)
    monkeypatch.setattr(
        native,
        "inspect_source_query_schema",
        lambda *_args: [SourceColumn("payload", native_type=None)],
    )
    monkeypatch.setattr(native, "_certify", lambda *_args: pytest.fail("certify unknown type"))
    monkeypatch.setattr(
        native, "_create_typed_stage", lambda *_args: pytest.fail("create before type validation")
    )

    with pytest.raises(NativeAssignmentError, match="native assignment population failed"):
        native.prepare_native_sql_assignment(options, schema, ["payload"], {"connection": object()})

    assert calls == []
    assert owned.source_certificate is owned.certificate is None
    assert native.query_assignment_recoverable(options)


def test_native_query_cleanup_only_drops_acknowledged_resources_in_reverse_order(monkeypatch):
    options, _schema, owned = checkpoint(monkeypatch)
    source = "sandbox.events__stage__source"
    unowned = "sandbox.events__stage__uncertain"
    stage = owned.certificate.stage_table
    owned.state.owned_stage_resources[:] = [(source, source), (stage, stage), (stage, stage)]
    owned.state.stage_table_candidates.extend([source, unowned])
    dropped, opened = [], []
    monkeypatch.setattr(native, "preserve_recovery_artifacts", lambda: False)
    monkeypatch.setattr(native, "get_sql_connection", opened.append)

    def run(alias, _role, operation, *, open_connection):
        open_connection(alias)
        return operation({"connection": object()})

    monkeypatch.setattr(native, "run_with_fresh_connection", run)
    monkeypatch.setattr(
        native,
        "drop_owned_resource",
        lambda _adapter, _conn, resource, **_kw: dropped.append(resource),
    )

    native.close_query_assignment(options)
    native.close_query_assignment(options)

    assert dropped == [stage, source]
    assert opened == ["gp", "gp"]
    assert unowned not in dropped


def test_native_query_structural_recovery_retains_every_checkpoint(monkeypatch):
    options, _schema, _owned = checkpoint(monkeypatch)
    monkeypatch.setattr(native, "preserve_recovery_artifacts", lambda: True)
    monkeypatch.setattr(native, "run_with_fresh_connection", lambda *_a, **_k: pytest.fail("drop"))

    native.close_query_assignment(options)


def test_native_query_cleanup_continues_after_failure_and_preserves_primary(monkeypatch):
    options, _schema, owned = checkpoint(monkeypatch)
    owned.state.owned_stage_resources.append(("sandbox.other", "sandbox.other"))
    dropped = []
    monkeypatch.setattr(native, "preserve_recovery_artifacts", lambda: False)

    def cleanup(_alias, _role, _operation, **_kwargs):
        dropped.append(True)
        message = "cleanup unavailable"
        raise ConnectionError(message)

    monkeypatch.setattr(native, "run_with_fresh_connection", cleanup)
    primary = RuntimeError("original operation")
    try:
        raise primary
    except RuntimeError:
        native.close_query_assignment(options)
    assert len(dropped) == 2


def test_native_query_semantic_cleanup_failure_surfaces_after_success(monkeypatch):
    options, _schema, _owned = checkpoint(monkeypatch)
    monkeypatch.setattr(native, "preserve_recovery_artifacts", lambda: False)

    def cleanup(*_args, **_kwargs):
        message = "cleanup unavailable"
        raise ConnectionError(message)

    monkeypatch.setattr(native, "run_with_fresh_connection", cleanup)
    with pytest.raises(ConnectionError, match="cleanup unavailable"):
        native.close_query_assignment(options)


@pytest.mark.parametrize("failure", ["collision", "uncertain"])
def test_native_query_create_distinguishes_collision_from_ambiguous_ownership(monkeypatch, failure):
    options, schema, owned = checkpoint(monkeypatch)
    owned.state.owned_stage_resources.clear()
    owned.state.stage_table_candidates.clear()
    owned.certificate = None
    names = iter(["sandbox.foreign", "sandbox.owned"])
    calls = []

    def execute(_connection, statement):
        calls.append(statement)
        if len(calls) == 1:
            error = ConnectionError("create acknowledgement unavailable")
            if failure == "collision":
                error.pgcode = "42P07"
            raise error

    adapter = SimpleNamespace(
        table_exists=lambda *_args, **_kwargs: False,
        build_materialize_transfer_source_sql=lambda stage, query, **_kw: (stage, query),
        execute_command=execute,
    )
    monkeypatch.setattr(native, "get_backend_adapter", lambda _backend: adapter)
    monkeypatch.setattr(native, "build_stage_table_name", lambda *_a, **_k: next(names))
    if failure == "uncertain":
        with pytest.raises(ConnectionError) as raised:
            native._materialize_native_query(options, owned, object())
        assert raised.value.analytics_toolkit_sql_stage_candidate == "sandbox.foreign"
        assert not raised.value.analytics_toolkit_sql_acknowledged_resources
        assert not owned.state.owned_stage_resources
        assert owned.state.stage_table_candidates == ["sandbox.foreign"]
    else:
        assert native._materialize_native_query(options, owned, object()) == "sandbox.owned"
        assert owned.state.stage_table_candidates == ["sandbox.owned"]
        assert owned.state.owned_stage_resources == [("sandbox.owned", "sandbox.owned")]
    assert calls[0][1] == options.source_sql
    assert native.requires_query_assignment(schema, "gp")


def test_native_query_exhausted_existing_collisions_never_claim_ownership(monkeypatch):
    options, _schema, owned = checkpoint(monkeypatch)
    owned.state.owned_stage_resources.clear()
    owned.state.stage_table_candidates.clear()
    adapter = SimpleNamespace(table_exists=lambda *_args, **_kwargs: True)
    monkeypatch.setattr(native, "get_backend_adapter", lambda _backend: adapter)
    monkeypatch.setattr(native, "build_stage_table_name", lambda *_a, **_k: "sandbox.foreign")
    with pytest.raises(ValueError, match="collision-free"):
        native._materialize_native_query(options, owned, object())
    assert not owned.state.owned_stage_resources
    assert not owned.state.stage_table_candidates
