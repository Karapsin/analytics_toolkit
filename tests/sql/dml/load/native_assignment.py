"""Native assignment checkpoints retain opaque inputs and local schema guarantees."""

from __future__ import annotations

import datetime as dt
import ipaddress
from copy import deepcopy
from decimal import Decimal

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.general import set_connections_path
from analytics_toolkit.sql.backends.gp.native_assignment import (
    NativeAssignmentCertificate,
    NativeAssignmentError,
)
from analytics_toolkit.sql.connection.config import get_connection_config
from analytics_toolkit.sql.connection.configuration_guards import check_connection_configuration
from analytics_toolkit.sql.dml.load import native_assignment as assignment
from analytics_toolkit.sql.dml.load.models import LoadOptions, LoadState
from analytics_toolkit.sql.execution.backend_options import backend_operation
from psycopg2.extensions import AsIs
from sqlglot import parse_one

from tests.sql._support.gp_cidr_casts import AssignmentConnection, native_cidr_target
from tests.sql._support.load_table import load_df_module


def state_with_checkpoint():
    certificate = NativeAssignmentCertificate(
        "public.owned_native_stage",
        (("id", "BIGINT"), ("network", "CIDR")),
        1,
        ("id",),
        (),
    )
    state = LoadState(True, True, overlap_stage_table=certificate.stage_table)
    state.native_assignment_checkpoint = certificate
    return state, certificate


def test_local_known_validation_and_conversion_preserve_opaque_native_object_identity():
    network = AsIs("'10.0.0.0/24'::cidr")
    index = pd.Index([9, 2], name="selected_row")
    frame = pd.DataFrame(
        {
            "network": pd.Series([network, None], index=index, dtype=object),
            "id": pd.Series([2**53 + 1, None], index=index, dtype=object),
            "amount": pd.Series([123.0, None], index=index, dtype=object),
        },
        index=index,
    )
    schema = {"network": "CIDR", "id": "BIGINT", "amount": "NUMERIC(5, 0)"}

    view, known = assignment.local_validation_view(frame, schema, "gp")
    profiles = assignment.validated_local_profiles(frame, schema, "gp")
    converted, metadata = assignment.convert_local_dataframe(frame, schema, "gp")

    assert list(view.columns) == list(known) == ["id", "amount"]
    assert view.index.equals(index)
    assert set(profiles) == {"id", "amount"}
    assert profiles["id"].maximum_integer == 2**53 + 1
    assert profiles["id"].null_count == profiles["amount"].null_count == 1
    assert list(converted.columns) == list(frame.columns)
    assert converted.index.equals(index)
    assert converted.iloc[0, 0] is network
    assert converted.iloc[1, 0] is None
    assert converted["id"].tolist() == [2**53 + 1, None]
    assert converted["amount"].tolist() == [Decimal(123), None]
    assert type(converted.iloc[0, 2]) is Decimal
    assert [item["column"] for item in metadata] == ["id", "amount"]
    assert all(item["certified_input_count"] == 2 for item in metadata)
    assert frame.iloc[0, 0] is network
    assert frame["amount"].tolist() == [123.0, None]


def test_known_timestamp_precision_waits_for_native_assignment_without_altering_exact_payload():
    stamp = dt.datetime(2025, 1, 1, 12, 34, 56, 123456, tzinfo=dt.timezone.utc)
    frame = pd.DataFrame({"id": pd.Series([2**53 + 1], dtype=object), "at": [stamp]})

    converted, metadata = assignment.convert_local_dataframe(
        frame, {"id": "BIGINT", "at": "TIMESTAMP(3) WITH TIME ZONE"}, "gp"
    )

    assert converted["id"].tolist() == [2**53 + 1]
    assert converted.iloc[0, 1] == stamp
    assert frame.iloc[0, 1].microsecond == 123456
    assert [item["column"] for item in metadata] == ["id", "at"]
    assert metadata[0]["certified_input_count"] == 1
    assert metadata[1]["reason"] == "native_assignment_pending"
    assert metadata[1]["native_assignment_required"]
    assert metadata[1]["certified_input_count"] == 0


@pytest.mark.parametrize(("native", "bad"), [("SMALLINT", 32768), ("BIGINT", 1.5)])
def test_invalid_last_known_value_cannot_hide_behind_an_opaque_native_column(native, bad):
    opaque = AsIs("'10.0.0.0/24'::cidr")
    frame = pd.DataFrame({"network": [opaque, opaque], "id": pd.Series([1, bad], dtype=object)})

    with pytest.raises(ValueError, match="authoritative integer"):
        assignment.validated_local_profiles(frame, {"network": "CIDR", "id": native}, "gp")

    assert frame.iloc[0, 0] is frame.iloc[1, 0] is opaque
    assert frame["id"].tolist() == [1, bad]


@pytest.mark.parametrize("columns", [["network", "missing"], ["network", "network"], [1, "1"]])
@pytest.mark.parametrize(
    "operation", [assignment.local_validation_view, assignment.convert_local_dataframe]
)
def test_local_views_retain_incoming_name_coverage_and_sql_name_uniqueness(columns, operation):
    frame = pd.DataFrame([[AsIs("'10.0.0.0/24'::cidr"), 1]], columns=columns)

    with pytest.raises(ValueError, match=r"authoritative SQL type|duplicate SQL names"):
        operation(frame, {"network": "CIDR", "1": "CIDR"}, "gp")

    assert list(frame.columns) == columns


def test_local_append_subset_does_not_invent_omitted_default_bearing_columns():
    opaque = AsIs("'10.0.0.0/24'::cidr")
    frame = pd.DataFrame({"network": [opaque], "id": [1]})
    schema = {"network": "CIDR", "id": "BIGINT", "defaulted": "TEXT"}

    converted, metadata = assignment.convert_local_dataframe(frame, schema, "gp")

    assert list(converted.columns) == ["network", "id"]
    assert converted.iloc[0, 0] is opaque
    assert [item["column"] for item in metadata] == ["id"]


def test_publication_options_require_a_complete_native_assignment_checkpoint():
    options = LoadOptions("gp", "gp", "public.target")

    with pytest.raises(RuntimeError, match="complete checkpoint"):
        assignment.checkpoint_options(options, LoadState(True, True))


def test_publication_options_bind_stored_schema_without_mutating_requested_schema_or_certificate():
    state, certificate = state_with_checkpoint()
    options = LoadOptions(
        "gp",
        "gp",
        "public.target",
        table_schema={"network": "TEXT", "id": "BIGINT"},
        use_parquet_staging=True,
        write_mode="truncate_insert",
        key_columns=["id"],
        retry_cnt=3,
        query_label="owned native payload",
    )

    published = assignment.checkpoint_options(options, state)

    assert published.table_schema == {"id": "BIGINT", "network": "CIDR"}
    assert list(published.table_schema) == ["id", "network"]
    assert published.use_parquet_staging is False
    assert (published.connection_key, published.destination_table, published.write_mode) == (
        "gp",
        "public.target",
        "truncate_insert",
    )
    assert (published.key_columns, published.retry_cnt, published.query_label) == (
        ["id"],
        3,
        "owned native payload",
    )
    published.table_schema["network"] = "TEXT"
    assert certificate.column_types == (("id", "BIGINT"), ("network", "CIDR"))
    assert options.table_schema == {"network": "TEXT", "id": "BIGINT"}
    assert options.use_parquet_staging is True


def test_certified_retry_reuses_stored_payload_without_creating_or_adapting_again(monkeypatch):
    state, certificate = state_with_checkpoint()
    options = LoadOptions("gp", "gp", "public.target")
    frame = pd.DataFrame({"id": [1], "network": [AsIs("'changed client value'")]})

    def unexpected_stage(*_args, **_kwargs):
        pytest.fail("A certified native payload must not be recreated or adapted again.")

    monkeypatch.setattr(assignment, "create_stage_table", unexpected_stage)
    for _ in range(2):
        assignment.prepare_assignment_checkpoint(
            options,
            state,
            object(),
            frame,
            {"id": "BIGINT", "network": "CIDR", "unused_default": "TEXT"},
        )

    assert state.native_assignment_checkpoint is certificate
    assert state.overlap_stage_table == certificate.stage_table
    assert state.stage_candidates == state.owned_stage_resources == []


@pytest.mark.parametrize(
    "schema", [{"id": "BIGINT", "network": "INET"}, {"id": "INTEGER", "network": "CIDR"}]
)
def test_certified_payload_cannot_be_reused_under_a_different_authoritative_type(schema):
    state, certificate = state_with_checkpoint()
    frame = pd.DataFrame({"id": [1], "network": [AsIs("'10.0.0.0/24'::cidr")]})

    with pytest.raises(ValueError, match="checkpoint schema differs"):
        assignment.prepare_assignment_checkpoint(
            LoadOptions("gp", "gp", "public.target"), state, object(), frame, schema
        )

    assert state.native_assignment_checkpoint is certificate


def test_certified_payload_cannot_be_reused_with_reordered_incoming_columns():
    state, certificate = state_with_checkpoint()
    frame = pd.DataFrame({"network": [AsIs("'10.0.0.0/24'::cidr")], "id": [1]})

    with pytest.raises(ValueError, match="checkpoint schema differs"):
        assignment.prepare_assignment_checkpoint(
            LoadOptions("gp", "gp", "public.target"),
            state,
            object(),
            frame,
            {"network": "CIDR", "id": "BIGINT"},
        )

    assert state.native_assignment_checkpoint is certificate


@pytest.mark.parametrize("requested", [None, {"id": "BIGINT", "amount": "NUMERIC(12, 2)"}])
def test_fully_known_schema_skips_native_assignment_stage_creation(monkeypatch, requested):
    state = LoadState(True, True)
    frame = pd.DataFrame({"id": [1], "amount": [Decimal("1.25")]})

    def unexpected_stage(*_args, **_kwargs):
        pytest.fail("Known-schema loads must retain the ordinary stage path.")

    monkeypatch.setattr(assignment, "create_stage_table", unexpected_stage)
    assignment.prepare_assignment_checkpoint(
        LoadOptions("gp", "gp", "public.target", table_schema=requested),
        state,
        object(),
        frame,
        {"id": "BIGINT", "amount": "NUMERIC(12, 2)"},
    )

    assert state.native_assignment_checkpoint is None
    assert state.overlap_stage_table is None
    assert state.stage_candidates == state.owned_stage_resources == []


def test_native_checkpoint_cannot_publish_to_reconfigured_alias_after_lost_target_ack(
    monkeypatch, write_sql_connections
):
    config = {
        "type": "gp",
        "host": "old-target.example",
        "port": 5432,
        "user": "user",
        "password": "password",
        "database": "warehouse",
    }
    set_connections_path(write_sql_connections({"gp": config}))
    old, replacement = native_cidr_target(monkeypatch), native_cidr_target(monkeypatch)
    replacement_seed = deepcopy(replacement.target.rows)
    old.lost_target_acknowledgements = 1
    certificates, switched = [], []
    certify = assignment.certify_assignment_checkpoint

    def observe_certificate(*args, **kwargs):
        certificate = certify(*args, **kwargs)
        certificates.append(certificate)
        return certificate

    class ChangeAliasAfterAcknowledgement(AssignmentConnection):
        def commit(self):
            try:
                super().commit()
            except ConnectionError:
                assert self.database is old
                assert len(certificates) == 1
                assert old.target.rows == [{"payload": ipaddress.ip_network("192.0.2.0/24")}]
                updated = {**config, "host": "replacement-target.example"}
                set_connections_path(write_sql_connections({"gp": updated}))
                switched.append(updated["host"])
                raise

    def connect(alias):
        current = get_connection_config(alias)
        database = old if current.host == config["host"] else replacement
        connection = ChangeAliasAfterAcknowledgement(database)
        database.opened.append(connection)
        return connection

    monkeypatch.setattr(assignment, "certify_assignment_checkpoint", observe_certificate)
    monkeypatch.setattr(load_df_module, "get_sql_connection", connect)
    frame = pd.DataFrame({"payload": ["192.0.2.0/24"]})
    failure = None
    try:
        sql.load_df(
            "gp",
            "sandbox.events",
            frame,
            write_mode="truncate_insert",
            retry_cnt=2,
            timeout_increment=0,
        )
    except Exception as error:  # noqa: BLE001 -- inspect both target scopes before the diagnostic.
        failure = error

    assert switched == ["replacement-target.example"]
    assert get_connection_config("gp").host == "replacement-target.example"
    assert replacement.live_mutations == []
    assert replacement.target.rows == replacement_seed
    assert isinstance(failure, NativeAssignmentError)
    assert "Destination configuration or namespace differs" in str(failure)
    assert failure.analytics_toolkit_sql_retry_safe is False
    assert len(certificates) == 1
    checkpoint = certificates[0]
    assert checkpoint.input_identity is not None
    assert failure.analytics_toolkit_sql_native_assignment_identity == checkpoint.input_identity
    assert checkpoint.stage_table in failure.analytics_toolkit_sql_acknowledged_resources
    stage = parse_one(f"SELECT * FROM {checkpoint.stage_table}", read="postgres").args["from_"].this
    assert old.table(stage).rows == [{"payload": ipaddress.ip_network("192.0.2.0/24")}]


@pytest.mark.parametrize("native", ["CIDR", "BIGINT"], ids=["opaque-freeze", "known-fast-path"])
def test_initial_open_snapshot_cannot_be_hidden_by_restoring_the_alias(
    write_sql_connections, native
):
    config = {
        "type": "gp",
        "host": "selected-target.example",
        "port": 5432,
        "user": "user",
        "password": "password",
        "database": "warehouse",
    }
    set_connections_path(write_sql_connections({"gp": config}))
    options = load_df_module._build_load_options(
        "gp",
        "sandbox.events",
        False,
        "truncate_insert",
        None,
        None,
        table_schema={"payload": native},
    )
    selected = get_connection_config("gp")
    set_connections_path(write_sql_connections({"gp": {**config, "host": "opened-target.example"}}))
    actually_opened = get_connection_config("gp")
    set_connections_path(write_sql_connections({"gp": config}))
    assert get_connection_config("gp") == selected
    assert actually_opened.host != selected.host

    with backend_operation() as operation:
        assignment.install_assignment_configuration_guard(options)
        try:
            check_connection_configuration(actually_opened)
            if native == "CIDR":
                with pytest.raises(NativeAssignmentError, match="initially selected"):
                    assignment.freeze_assignment_identity(options)
                assert "native_assignment_input_identity" not in options.destination_context
            else:
                state = LoadState(True, True)
                assignment.prepare_assignment_checkpoint(
                    options,
                    state,
                    object(),
                    pd.DataFrame({"payload": [42]}),
                    {"payload": native},
                )
                assert state.native_assignment_checkpoint is None
                assert state.stage_candidates == state.owned_stage_resources == []
        finally:
            assignment.remove_assignment_configuration_guard(options)
        assert not operation.resources.get("connection_configuration_guards")


def _configured_guard_options(write_sql_connections, alias="gp"):
    selected = {
        "type": "gp",
        "host": "selected-target.example",
        "port": 5432,
        "user": "user",
        "password": "password",
        "database": "warehouse",
    }
    foreign = {**selected, "host": "foreign-target.example"}
    set_connections_path(write_sql_connections({"gp": selected, "other_gp": foreign}))
    return load_df_module._build_load_options(
        alias,
        "sandbox.events",
        False,
        "truncate_insert",
        None,
        None,
        table_schema={"payload": "CIDR"},
    )


def test_unscoped_guard_installation_and_repeated_removal_leave_no_hidden_registration():
    options = LoadOptions("gp", "gp", "sandbox.events")
    assert assignment.current_backend_operation() is None

    assignment.install_assignment_configuration_guard(options)
    assignment.remove_assignment_configuration_guard(options)
    assignment.remove_assignment_configuration_guard(options)

    assert options.destination_context == {}
    with backend_operation() as operation:
        assert not operation.resources.get("connection_configuration_guards")
        assignment.remove_assignment_configuration_guard(options)
        assert not operation.resources.get("connection_configuration_guards")


def test_frozen_guard_ignores_other_aliases_and_validates_legacy_and_resolved_notifications(
    write_sql_connections,
):
    options = _configured_guard_options(write_sql_connections)
    selected, foreign = get_connection_config("gp"), get_connection_config("other_gp")
    identity = assignment.freeze_assignment_identity(options)

    with backend_operation() as operation:
        assignment.install_assignment_configuration_guard(options)
        key = options.destination_context["native_assignment_guard_key"]
        guard = operation.resources["connection_configuration_guards"][key]
        try:
            check_connection_configuration(foreign)
            guard("  GP  ")
            check_connection_configuration(selected)
            assert assignment.assignment_input_identity(options) == identity
            assert (
                "native_assignment_opened_configuration_changed" not in options.destination_context
            )
        finally:
            assignment.remove_assignment_configuration_guard(options)
        assignment.remove_assignment_configuration_guard(options)
        assert not operation.resources.get("connection_configuration_guards")


@pytest.mark.parametrize(
    "scope", ["missing-certificate-scope", "foreign-certificate", "unfrozen-run"]
)
def test_certified_native_values_require_the_same_frozen_run_identity_before_publication(
    monkeypatch,
    write_sql_connections,
    scope,
):
    options = _configured_guard_options(write_sql_connections)
    database = native_cidr_target(monkeypatch)
    seed = deepcopy(database.target.rows)
    connection = AssignmentConnection(database)
    frame = pd.DataFrame({"payload": ["192.0.2.0/24"]})
    state = LoadState(True, True)
    assignment.prepare_assignment_checkpoint(
        options,
        state,
        connection,
        frame,
        {"payload": "CIDR"},
    )
    original = state.native_assignment_checkpoint
    assert original is not None
    assert original.input_identity is not None

    if scope == "unfrozen-run":
        publication_options = _configured_guard_options(write_sql_connections)
    else:
        publication_options = options
        foreign_identity = None
        if scope == "foreign-certificate":
            foreign_options = load_df_module._build_load_options(
                "other_gp",
                "sandbox.events",
                False,
                "truncate_insert",
                None,
                None,
                table_schema={"payload": "CIDR"},
            )
            foreign_identity = assignment.freeze_assignment_identity(foreign_options)
        state.native_assignment_checkpoint = assignment.certify_assignment_checkpoint(
            load_df_module.get_backend_adapter("gp"),
            connection,
            original.stage_table,
            dict(original.column_types),
            expected_rows=1,
            input_identity=foreign_identity,
        )
    commands = list(database.commands)

    with pytest.raises(NativeAssignmentError, match="not bound to the frozen destination") as error:
        assignment.insert_checkpoint_rows(publication_options, state, connection)

    assert error.value.analytics_toolkit_sql_retry_safe is False
    assert database.commands == commands
    assert database.live_mutations == []
    assert database.target.rows == seed
    stage = parse_one(f"SELECT * FROM {original.stage_table}", read="postgres").args["from_"].this
    assert database.table(stage).rows == [{"payload": ipaddress.ip_network("192.0.2.0/24")}]
    assert frame.iloc[0, 0] == "192.0.2.0/24"


def test_native_publication_without_a_checkpoint_cannot_submit_target_sql(monkeypatch):
    database = native_cidr_target(monkeypatch)
    seed = deepcopy(database.target.rows)

    with pytest.raises(RuntimeError, match="complete checkpoint"):
        assignment.insert_checkpoint_rows(
            LoadOptions("gp", "gp", "sandbox.events"),
            LoadState(True, True),
            AssignmentConnection(database),
        )

    assert database.commands == database.bound_rows == database.live_mutations == []
    assert database.target.rows == seed


def test_empty_native_assignment_replacement_certifies_zero_rows_without_driver_adaptation(
    monkeypatch,
):
    database = native_cidr_target(monkeypatch)
    assert database.target.rows
    frame = pd.DataFrame({"payload": pd.Series([], dtype=object)})

    assert (
        sql.load_df(
            "gp",
            "sandbox.events",
            frame,
            write_mode="truncate_insert",
            table_schema={"payload": "CIDR"},
            empty_source_policy="replace",
            retry_cnt=1,
            timeout_increment=0,
        )
        == 0
    )

    assert database.target.rows == []
    assert database.target.schema == {"payload": "CIDR"}
    assert database.bound_rows == []
    assert set(database.tables) == {("sandbox", "events")}
    assert frame.empty
    assert list(frame.columns) == ["payload"]
