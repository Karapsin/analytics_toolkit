"""Query creation certifies the real GP native SQL before live replacement."""

from __future__ import annotations

import ipaddress
from copy import deepcopy

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.gp.native_assignment import NativeAssignmentError
from analytics_toolkit.sql.connection.config import get_connection_config
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.dml import query_input
from analytics_toolkit.sql.dml.transfer.flow.adaptive_input import (
    configuration_identity_from_configs,
)
from analytics_toolkit.sql.dml.transfer.runtime.models import TransferOptions, TransferStageState
from analytics_toolkit.sql.execution.plans import SqlOperationMetadata

from tests.sql._support.query_native_assignment import (
    QueryNativeDatabase,
    create,
    install_python_query_native,
    install_query_native,
    truncate_python_checkpoint,
)
from tests.sql._support.transfer_native_assignment import CHECKED_DOMAIN, SHORT_DOMAIN


def create_query(*, query=None, **options):
    return sql.create_table(
        "gp",
        "sandbox.events",
        sql=query or "SELECT CAST(payload AS CIDR[]) AS payload FROM sandbox.events",
        insert_data=True,
        drop_if_exists=options.pop("drop_if_exists", True),
        retry_cnt=options.pop("retry_cnt", 1),
        timeout_increment=0,
        return_metadata=True,
        **options,
    )


def create_scalar_query(native_type="CIDR", *, query=None, **options):
    return create.create_table_from_sql(
        "gp",
        "sandbox.events",
        query or f"SELECT CAST(payload AS {native_type}) AS payload FROM sandbox.events",
        table_schema={"payload": native_type},
        drop_target_if_exists=True,
        retry_cnt=options.pop("retry_cnt", 1),
        timeout_increment=0,
        return_metadata=True,
        **options,
    )


def test_gp_query_native_cidr_array_certifies_actual_self_source_before_replacement(monkeypatch):
    values = ["192.0.2.0/24", "2001:db8::/64", None]
    database, _databases, _connect = install_query_native(
        monkeypatch,
        [([values[0], None],), ([values[1]],), (None,)],
        native_type="TEXT[]",
    )

    result = create_query()

    assert result.rows == 3
    assert database.target.rows == [
        {"payload": [ipaddress.ip_network(values[0]), None]},
        {"payload": [ipaddress.ip_network(values[1])]},
        {"payload": None},
    ]
    assert database.python_reads == []
    assert database.bound_rows == []
    assert len(database.source_evaluations) == 1
    assert result.metadata.input_count == 3
    assert any(
        item.get("native_assignment_validation") == "certified"
        for item in result.metadata.type_resolutions
    )
    assert set(database.tables) == {("sandbox", "events"), ("sandbox", "source_rows")}


def test_gp_query_native_scalar_cidr_explicit_authority_preserves_exact_values(monkeypatch):
    values = ["192.0.2.0/24", "2001:db8::/64", None]
    database, _databases, _connect = install_query_native(
        monkeypatch, [(value,) for value in values]
    )

    result = create_scalar_query()

    assert result.rows == 3
    assert database.target.schema == {"payload": "CIDR"}
    assert database.target.rows == [
        {"payload": ipaddress.ip_network(value) if value is not None else None} for value in values
    ]
    assert len(database.source_evaluations) == 1
    assert database.python_reads == []
    assert database.bound_rows == []


def test_gp_query_native_invalid_last_cast_preserves_live_seed_and_private_values(
    monkeypatch, caplog
):
    private = "192.0.2.1/24"
    database, _databases, _connect = install_query_native(
        monkeypatch, [("192.0.2.0/24",), (private,)]
    )
    original = deepcopy(database.target.rows)

    with pytest.raises(NativeAssignmentError, match="native assignment") as error:
        create_scalar_query(retry_cnt=3)

    assert database.target.rows == original
    assert database.live_mutations == []
    assert private not in str(error.value)
    assert private not in caplog.text
    assert set(database.tables) == {("sandbox", "events"), ("sandbox", "source_rows")}


def test_gp_query_native_explicit_domain_projection_retains_sql_cast_semantics(monkeypatch):
    database, _databases, _connect = install_query_native(monkeypatch, [("long",), (None,)])

    result = create_scalar_query(
        SHORT_DOMAIN, query=f"SELECT CAST(payload AS {SHORT_DOMAIN}) AS payload FROM sandbox.events"
    )

    assert result.rows == 2
    assert database.target.rows == [{"payload": "lon"}, {"payload": None}]
    assert database.python_reads == []
    assert database.bound_rows == []
    assert any(value == "lon" for _native, value in database.cast_values)


def test_gp_query_native_cross_alias_projection_runs_in_target_context(monkeypatch):
    target, databases, _connect = install_query_native(
        monkeypatch, [(["192.0.2.0/24"],)], native_type="TEXT[]"
    )
    source = QueryNativeDatabase([(["198.51.100.0/24"],)], native_type="TEXT[]")
    databases["gp_sandbox"] = source

    result = create_query(source_db="gp_sandbox")

    assert result.rows == 1
    assert target.target.rows == [{"payload": [ipaddress.ip_network("192.0.2.0/24")]}]
    assert source.target.rows == [{"payload": ["198.51.100.0/24"]}]
    assert target.snapshot_creates
    assert not source.snapshot_creates
    assert len(target.source_evaluations) == 1
    assert source.source_evaluations == []
    assert not target.python_reads


def test_gp_query_native_lost_ack_reuses_certified_sql_payload(monkeypatch):
    database, _databases, _connect = install_query_native(
        monkeypatch, [(["192.0.2.0/24"],), (["2001:db8::/64"],)], native_type="TEXT[]"
    )
    database.fail_target_ack = 1

    result = create_query(retry_cnt=2)

    assert result.rows == 2
    assert database.target.rows == [
        {"payload": [ipaddress.ip_network("192.0.2.0/24")]},
        {"payload": [ipaddress.ip_network("2001:db8::/64")]},
    ]
    assert len(database.snapshot_creates) == 1
    assert len(database.source_evaluations) == 1
    assert database.python_reads == []
    assert database.bound_rows == []
    assert result.metadata.data_write_replay_possible is True
    assert set(database.tables) == {("sandbox", "events"), ("sandbox", "source_rows")}


def test_gp_query_native_snapshot_header_mismatch_prevents_live_replacement(monkeypatch):
    database, _databases, _connect = install_query_native(
        monkeypatch, [(["192.0.2.0/24"],)], native_type="TEXT[]"
    )
    original = deepcopy(database.target.rows)
    database.snapshot_header_rename = "different_payload"

    with pytest.raises(NativeAssignmentError, match="native assignment"):
        create_query()

    assert database.snapshot_headers_seen == [("payload", "different_payload")]
    assert len(database.snapshot_creates) == len(database.source_evaluations) == 1
    assert database.target.rows == original
    assert database.live_mutations == database.private_query_attempts == []
    assert database.bound_rows == database.python_reads == []
    assert set(database.tables) == {("sandbox", "events"), ("sandbox", "source_rows")}


def test_gp_query_native_private_assignment_retry_reuses_certified_source(monkeypatch):
    database, _databases, _connect = install_query_native(
        monkeypatch, [(["192.0.2.0/24", None],)], native_type="TEXT[]"
    )
    database.fail_private_query_assignments = 1

    result = create_query(retry_cnt=2)

    assert result.rows == result.metadata.input_count == 1
    assert database.private_failure_live_mutations == [[]]
    assert len(database.private_query_attempts) == 2
    assert len(database.snapshot_creates) == len(database.source_evaluations) == 1
    assert database.reject_source_reexecution is True
    assert database.target.rows == [{"payload": [ipaddress.ip_network("192.0.2.0/24"), None]}]
    assert database.bound_rows == database.python_reads == []
    assert set(database.tables) == {("sandbox", "events"), ("sandbox", "source_rows")}


@pytest.mark.parametrize("exists", [False, True], ids=["new-target", "empty-replacement"])
def test_gp_query_native_empty_input_still_certifies_native_schema(monkeypatch, exists):
    database, _databases, _connect = install_query_native(
        monkeypatch, [], native_type="TEXT[]", exists=exists
    )

    result = create_query(
        query="SELECT CAST(payload AS CIDR[]) AS payload FROM sandbox.source_rows",
        drop_if_exists=exists,
    )

    assert result.rows == 0
    assert result.metadata.input_count == 0
    assert database.target.rows == []
    assert database.target.schema == {"payload": "CIDR[]"}
    assert len(database.snapshot_creates) == 1
    assert database.bound_rows == []
    assert database.python_reads == []
    assert set(database.tables) == {("sandbox", "events"), ("sandbox", "source_rows")}


def test_gp_query_native_fresh_creation_retains_cidr_values(monkeypatch):
    database, _databases, _connect = install_query_native(
        monkeypatch, [(["192.0.2.0/24"],)], native_type="TEXT[]", exists=False
    )

    result = create_query(
        query="SELECT CAST(payload AS CIDR[]) AS payload FROM sandbox.source_rows",
        drop_if_exists=False,
    )

    assert result.rows == 1
    assert database.target.schema == {"payload": "CIDR[]"}
    assert database.target.rows == [{"payload": [ipaddress.ip_network("192.0.2.0/24")]}]
    assert len(database.snapshot_creates) == 1


def test_gp_query_native_if_not_exists_skips_inspection_and_population(monkeypatch):
    database, _databases, _connect = install_query_native(
        monkeypatch, [(["private invalid"],)], native_type="TEXT[]"
    )
    original = deepcopy(database.target.rows)

    result = create_query(if_not_exists=True, drop_if_exists=False)

    assert result.rows is None
    assert result.metadata.statement_count == 0
    assert database.target.rows == original
    assert database.query_inspections == []
    assert database.source_evaluations == []
    assert database.snapshot_creates == []
    assert database.live_mutations == []


def test_gp_query_native_existing_target_default_fails_before_checkpoint(monkeypatch):
    database, _databases, _connect = install_query_native(
        monkeypatch, [(["192.0.2.0/24"],)], native_type="TEXT[]"
    )
    original = deepcopy(database.target.rows)

    with pytest.raises(InvalidSqlInputError, match="already exists"):
        create_query(drop_if_exists=False)

    assert database.target.rows == original
    assert database.source_evaluations == []
    assert database.snapshot_creates == []
    assert database.python_reads == []
    assert database.live_mutations == []


def test_gp_query_native_final_domain_check_runs_before_target_drop(monkeypatch, caplog):
    database, _databases, _connect = install_query_native(monkeypatch, [("ok",), ("bad",)])
    original = deepcopy(database.target.rows)

    with pytest.raises(NativeAssignmentError, match="native assignment") as error:
        create.create_table_from_sql(
            "gp",
            "sandbox.events",
            "SELECT payload FROM sandbox.source_rows",
            table_schema={"payload": CHECKED_DOMAIN},
            drop_target_if_exists=True,
            retry_cnt=3,
            timeout_increment=0,
        )

    assert len(database.snapshot_creates) == 1
    assert database.target.rows == original
    assert database.live_mutations == []
    assert "'bad'" not in str(error.value)
    assert "'bad'" not in caplog.text
    assert database.bound_rows == []
    assert set(database.tables) == {("sandbox", "events"), ("sandbox", "source_rows")}


@pytest.mark.parametrize("native_type", ["CIDR", SHORT_DOMAIN, CHECKED_DOMAIN])
def test_gp_query_native_dry_plan_has_pending_validation_without_materialization(
    monkeypatch, native_type
):
    database, _databases, _connect = install_query_native(monkeypatch, [("private",)])

    plan = create_scalar_query(
        native_type,
        query=f"SELECT CAST(payload AS {native_type}) AS payload FROM sandbox.events",
        dry_run=True,
    )

    assert plan.options["pending_input_validation"] is True
    assert plan.options["native_assignment_validation_pending"] is True
    assert database.opened == []
    assert database.commands == []
    assert database.snapshot_creates == []


def test_gp_query_python_native_assignment_rejects_domain_overflow_without_sql_cast(
    monkeypatch, write_sql_connections
):
    database = install_python_query_native(monkeypatch, write_sql_connections, [("long",)])
    original = deepcopy(database.target.rows)

    with pytest.raises(NativeAssignmentError, match="native assignment"):
        create.create_table_from_sql(
            "ch",
            "sandbox.events",
            "SELECT payload FROM sandbox.source_rows",
            table_db="gp",
            table_schema={"payload": SHORT_DOMAIN},
            drop_target_if_exists=True,
            retry_cnt=1,
            timeout_increment=0,
        )

    assert database.target.rows == original
    assert database.live_mutations == []
    assert database.bound_rows == [("long",)]
    assert not database.cast_values


def test_gp_query_python_native_checkpoint_replays_without_source_or_adaptation(
    monkeypatch, write_sql_connections
):
    values = ["192.0.2.0/24", "2001:db8::/64", None]
    database = install_python_query_native(
        monkeypatch, write_sql_connections, [(value,) for value in values]
    )
    database.fail_target_ack = 1

    result = create.create_table_from_sql(
        "ch",
        "sandbox.events",
        "SELECT payload FROM sandbox.source_rows",
        table_db="gp",
        table_schema={"payload": "CIDR"},
        drop_target_if_exists=True,
        retry_cnt=2,
        timeout_increment=0,
        return_metadata=True,
    )

    assert result.rows == 3
    assert database.target.rows == [
        {"payload": ipaddress.ip_network(value) if value is not None else None} for value in values
    ]
    assert len(database.python_reads) == 1
    assert database.bound_rows == [(value,) for value in values]
    assert database.snapshot_creates == []
    assert result.metadata.input_count == 3
    assert set(database.tables) == {("sandbox", "events"), ("sandbox", "source_rows")}


def test_gp_query_python_empty_input_certifies_native_stage_before_replacement(
    monkeypatch, write_sql_connections
):
    database = install_python_query_native(monkeypatch, write_sql_connections, [])
    database.target.rows = [{"payload": "seed survives until certification"}]

    result = create.create_table_from_sql(
        "ch",
        "sandbox.events",
        "SELECT payload FROM sandbox.source_rows",
        table_db="gp",
        table_schema={"payload": "CIDR"},
        drop_target_if_exists=True,
        retry_cnt=1,
        timeout_increment=0,
        return_metadata=True,
    )

    assert result.rows == 0
    assert result.metadata.input_count == 0
    assert database.target.rows == []
    assert database.target.schema == {"payload": "CIDR"}
    assert len(database.python_reads) == 1
    assert database.bound_rows == []
    assert database.snapshot_creates == []
    assert set(database.tables) == {("sandbox", "events"), ("sandbox", "source_rows")}


def test_gp_query_python_truncated_stage_cannot_publish_complete_input(
    monkeypatch, write_sql_connections
):
    rows = [("192.0.2.0/24",), ("2001:db8::/64",)]
    database = install_python_query_native(monkeypatch, write_sql_connections, rows)
    original = deepcopy(database.target.rows)
    truncate_python_checkpoint(monkeypatch)

    with pytest.raises(NativeAssignmentError, match="native assignment"):
        create.create_table_from_sql(
            "ch",
            "sandbox.events",
            "SELECT payload FROM sandbox.source_rows",
            table_db="gp",
            table_schema={"payload": "CIDR"},
            drop_target_if_exists=True,
            retry_cnt=1,
            timeout_increment=0,
        )

    assert database.python_selected_rows == rows
    assert database.bound_rows == rows[:1]
    assert database.target.rows == original
    assert database.live_mutations == database.private_query_attempts == []
    assert set(database.tables) == {("sandbox", "events"), ("sandbox", "source_rows")}


def test_gp_query_pending_native_payload_cannot_replay_without_a_certificate(monkeypatch):
    resolution = configuration_identity_from_configs(
        [get_connection_config("ch"), get_connection_config("gp")]
    )
    resolution["native_assignment_pending"] = True
    options = TransferOptions(
        from_db_key="ch",
        from_db_backend="ch",
        to_db_key="gp",
        to_db_backend="gp",
        source_sql="SELECT payload FROM source_rows",
        target_table="sandbox.events",
        input_resolution=resolution,
    )
    prepared = query_input.PreparedQueryInput(
        options,
        TransferStageState(target_exists=True, source_columns=["payload"]),
        {"payload": "CIDR"},
        ["payload"],
        True,
    )
    writes = []
    monkeypatch.setattr(query_input, "insert_rows_batch", lambda *_a, **_kw: writes.append(True))

    with pytest.raises(ValueError, match=r"(?i)(certif|checkpoint)"):
        query_input.replay_query_input(prepared, {"connection": object()}, SqlOperationMetadata())

    assert writes == []
