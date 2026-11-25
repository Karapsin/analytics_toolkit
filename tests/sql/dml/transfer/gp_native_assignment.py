"""Unknown GP native transfers certify assignment before publishing live rows."""

from __future__ import annotations

import ipaddress
from copy import deepcopy

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.gp.native_assignment import NativeAssignmentError
from analytics_toolkit.sql.dml.transfer.runtime import retry as transfer_retry
from psycopg2 import OperationalError, errors
from psycopg2.extensions import ISQLQuote, QuotedString, adapters
from sqlglot import exp

from tests.conftest import DEFAULT_SQL_CONNECTIONS
from tests.sql._support.transfer_native_assignment import (
    CHECKED_DOMAIN,
    SHORT_DOMAIN,
    install_native_transfer,
)


def transfer(*, write_mode="truncate_insert", **options):
    return sql.transfer(
        options.pop("from_db", "ch"),
        "gp",
        from_sql=options.pop("from_sql", "SELECT payload FROM source_rows"),
        to_table="sandbox.events",
        write_mode=write_mode,
        retry_cnt=options.pop("retry_cnt", 1),
        timeout_increment=options.pop("timeout_increment", 0),
        full_retry_cnt=options.pop("full_retry_cnt", 1),
        full_timeout_increment=0,
        batch_size=1,
        min_batch_size=1,
        adaptive_batch_size=False,
        ignore_source_staging=True,
        estimate_total_rows=False,
        validate_row_count=False,
        **options,
    )


@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
def test_gp_native_transfer_cidr_assignment_uses_complete_owned_stage(monkeypatch, explicit):
    values = ["192.0.2.0/24", "2001:db8::/64", None]
    database = install_native_transfer(
        monkeypatch, rows=[(value,) for value in values], target_schema={"payload": "CIDR"}
    )
    options = {"table_schema": {"payload": "CIDR"}} if explicit else {}

    assert transfer(**options) == 3

    assert database.target.rows == [
        {"payload": ipaddress.ip_network(value) if value is not None else None} for value in values
    ]
    assert database.bound_rows == [(value,) for value in values]
    assert len(database.reads) == 1
    assert set(database.tables) == {("sandbox", "events")}


@pytest.mark.parametrize("exists", [False, True], ids=["new", "existing"])
def test_gp_native_transfer_late_invalid_cidr_prevents_live_creation_or_clear(
    monkeypatch, caplog, exists
):
    database = install_native_transfer(
        monkeypatch,
        rows=[("192.0.2.0/24",), ("192.0.2.1/24",)],
        target_schema={"payload": "CIDR"},
        exists=exists,
    )
    original = deepcopy(database.tables)

    with pytest.raises(NativeAssignmentError, match="native assignment") as error:
        transfer(table_schema={"payload": "CIDR"})

    assert database.live_mutations == []
    assert "192.0.2.1/24" not in str(error.value)
    assert "192.0.2.1/24" not in caplog.text
    assert len(database.reads) == 1
    assert database.tables.keys() == original.keys()
    if exists:
        assert database.target.rows == original[("sandbox", "events")].rows


def test_gp_native_transfer_unknown_domain_uses_assignment_before_live_mutation(monkeypatch):
    database = install_native_transfer(
        monkeypatch, rows=[("ok",), ("abcd",)], target_schema={"payload": SHORT_DOMAIN}
    )
    original = deepcopy(database.target.rows)

    with pytest.raises(NativeAssignmentError, match="native assignment"):
        transfer(table_schema={"payload": SHORT_DOMAIN})

    assert database.live_mutations == []
    assert database.target.rows == original
    assert database.bound_rows == [("ok",), ("abcd",)]
    assert set(database.tables) == {("sandbox", "events")}


def test_gp_native_transfer_macaddr_remains_native_without_a_local_family_whitelist(monkeypatch):
    value = "08:00:2b:01:02:03"
    database = install_native_transfer(
        monkeypatch, rows=[(value,)], target_schema={"payload": "MACADDR"}
    )

    assert transfer() == 1

    assert database.target.rows == [{"payload": bytes.fromhex("08002b010203")}]
    assert database.bound_rows == [(value,)]
    assert len(database.reads) == 1


@pytest.mark.parametrize("invalid", [False, True], ids=["native-array", "invalid-last-leaf"])
def test_gp_native_transfer_unknown_array_keeps_real_driver_cast_and_null_leaves(
    monkeypatch, invalid
):
    value = ["192.0.2.0/24", None, "192.0.2.1/24" if invalid else "2001:db8::/64"]
    database = install_native_transfer(
        monkeypatch,
        rows=[(value,)],
        target_schema={"payload": "CIDR[]"},
        source_types={"payload": "Array(Nullable(String))"},
    )
    original = deepcopy(database.target.rows)

    if invalid:
        with pytest.raises(NativeAssignmentError, match="native assignment"):
            transfer()
        assert database.target.rows == original
        assert database.live_mutations == []
    else:
        assert transfer() == 1
        assert database.target.rows == [
            {
                "payload": [
                    ipaddress.ip_network("192.0.2.0/24"),
                    None,
                    ipaddress.ip_network("2001:db8::/64"),
                ]
            }
        ]
    assert database.bound_rows == [(value,)]
    assert any(
        "CAST(ARRAY[" in statement and "AS pg_catalog.cidr[])" in statement
        for statement in database.commands
    )
    assert database.parameter_catalog_reads


def test_gp_native_transfer_array_domain_typmod_is_checked_by_assignment(monkeypatch):
    value = ["ok", "abcd"]
    database = install_native_transfer(
        monkeypatch,
        rows=[(value,)],
        target_schema={"payload": SHORT_DOMAIN + "[]"},
        source_types={"payload": "Array(String)"},
    )
    original = deepcopy(database.target.rows)

    with pytest.raises(NativeAssignmentError, match="native assignment"):
        transfer()

    assert database.target.rows == original
    assert database.live_mutations == []
    assert database.bound_rows == [(value,)]
    assert database.parameter_catalog_reads
    assert any(
        "CAST(ARRAY[" in statement and "AS pg_catalog.varchar[])" in statement
        for statement in database.commands
    )


def test_gp_native_transfer_all_keys_reach_input_barrier_before_invalid_native_publication(
    monkeypatch,
):
    database = install_native_transfer(
        monkeypatch,
        rows=[(1, "192.0.2.0/24"), (2, "192.0.2.1/24")],
        target_schema={"id": "INTEGER", "payload": "CIDR"},
        source_types={"id": "Int32", "payload": "Nullable(String)"},
    )
    original = deepcopy(database.target.rows)

    with pytest.raises(NativeAssignmentError, match="native assignment"):
        transfer(
            from_sql="SELECT id, payload FROM source_rows WHERE {id}",
            table_schema={"id": "INTEGER", "payload": "CIDR"},
            transfer_keys="id",
            transfer_key_values=[1, 2],
            read_concurrency=2,
            write_concurrency=2,
        )

    assert database.target.rows == original
    assert database.live_mutations == []
    assert len(database.reads) == len(set(database.reads)) == 2
    assert database.bound_rows == [(1, "192.0.2.0/24"), (2, "192.0.2.1/24")]


def test_gp_native_transfer_certifies_the_actual_final_explicit_cast_chain(monkeypatch):
    database = install_native_transfer(
        monkeypatch, rows=[("abcd",)], target_schema={"payload": SHORT_DOMAIN}
    )

    assert transfer(table_schema={"payload": "TEXT"}) == 1

    assert database.target.rows == [{"payload": "abc"}]
    assert (SHORT_DOMAIN, "abc") in database.cast_values
    assert database.bound_rows == [("abcd",)]
    assert len(database.reads) == 1


def test_gp_native_transfer_final_domain_check_failure_keeps_original_target(monkeypatch):
    database = install_native_transfer(
        monkeypatch, rows=[("bad",)], target_schema={"payload": CHECKED_DOMAIN}
    )
    original = deepcopy(database.target.rows)

    with pytest.raises(NativeAssignmentError, match="native assignment"):
        transfer(table_schema={"payload": "TEXT"})

    assert database.target.rows == original
    assert database.live_mutations == []
    assert database.bound_rows == [("bad",)]


def test_gp_native_transfer_native_duplicate_keys_reject_before_publication(monkeypatch):
    database = install_native_transfer(
        monkeypatch,
        rows=[("2001:DB8:0:0::/64",), ("2001:db8::/64",)],
        target_schema={"payload": "CIDR"},
    )
    original = deepcopy(database.target.rows)

    with pytest.raises(ValueError, match=r"[Dd]uplicate"):
        transfer(key_columns=["payload"])

    assert database.target.rows == original
    assert database.live_mutations == []
    assert len(database.bound_rows) == 2


def test_gp_native_transfer_driver_adapted_null_key_rejects_before_publication(monkeypatch):
    database = install_native_transfer(
        monkeypatch, rows=[("driver-null",)], target_schema={"payload": "CIDR"}
    )
    original = deepcopy(database.target.rows)

    class NativeNull(QuotedString):
        def getquoted(self):
            return b"NULL"

    monkeypatch.setitem(adapters, (str, ISQLQuote), NativeNull)

    with pytest.raises(ValueError, match=r"[Nn][Uu][Ll][Ll]"):
        transfer(key_columns=["payload"])

    assert database.target.rows == original
    assert database.live_mutations == []
    assert database.bound_rows == [("driver-null",)]


def test_gp_native_transfer_certified_checkpoint_replays_without_read_or_adaptation(monkeypatch):
    values = ["192.0.2.0/24", "2001:db8::/64"]
    database = install_native_transfer(
        monkeypatch, rows=[(value,) for value in values], target_schema={"payload": "CIDR"}
    )
    adapted = []

    class CountedString(QuotedString):
        def __init__(self, value):
            super().__init__(value)
            self.value = value

        def getquoted(self):
            adapted.append(self.value)
            return super().getquoted()

    monkeypatch.setitem(adapters, (str, ISQLQuote), CountedString)
    database.fail_target_ack = 1

    assert transfer(table_schema={"payload": "CIDR"}, full_retry_cnt=2) == 2

    assert database.target.rows == [{"payload": ipaddress.ip_network(value)} for value in values]
    assert adapted == values
    assert database.bound_rows == [(value,) for value in values]
    assert len(database.reads) == database.source_opens == 1
    assert database.source_unavailable
    assert set(database.tables) == {("sandbox", "events")}


@pytest.mark.parametrize(
    ("sqlstate", "native", "invalid"),
    [("23514", CHECKED_DOMAIN, "bad"), ("22001", SHORT_DOMAIN, "abcd")],
    ids=["domain-check", "domain-typmod"],
)
def test_gp_native_transfer_inner_retry_stops_deterministic_assignment_failures(
    monkeypatch, sqlstate, native, invalid
):
    database = install_native_transfer(
        monkeypatch, rows=[("ok",), (invalid,)], target_schema={"payload": native}
    )
    original = deepcopy(database.target.rows)
    decode = database.decode_value
    delays = []

    class NativeServerFailure(errors.lookup(sqlstate)):
        @property
        def pgcode(self):
            return sqlstate

    def fail_native_assignment(node, native_type):
        if isinstance(node, exp.Literal) and node.this == invalid:
            message = "Native assignment rejected the private final input value."
            raise NativeServerFailure(message)
        return decode(node, native_type)

    monkeypatch.setattr(database, "decode_value", fail_native_assignment)
    monkeypatch.setattr(transfer_retry.time, "sleep", delays.append)

    with pytest.raises(NativeAssignmentError, match=f"SQLSTATE {sqlstate}"):
        transfer(retry_cnt=3, timeout_increment=0.01)

    assert (database.bound_rows.count((invalid,)), delays) == (1, [])
    assert len(database.private_value_attempts) == 2
    assert database.target.rows == original
    assert database.live_mutations == []
    assert len(database.reads) == 1
    assert set(database.tables) == {("sandbox", "events")}


def test_gp_native_transfer_inner_retry_replays_transient_failure_with_configured_delay(
    monkeypatch,
):
    value = "192.0.2.0/24"
    database = install_native_transfer(
        monkeypatch, rows=[(value,)], target_schema={"payload": "CIDR"}
    )
    insert = database._insert
    delays = []
    attempts = []

    def fail_before_private_mutation(tree, identity, params):
        if isinstance(tree.expression, exp.Values):
            attempts.append(identity)
            if len(attempts) == 1:
                message = "Temporary native driver connection failure before row mutation."
                raise OperationalError(message)
        return insert(tree, identity, params)

    monkeypatch.setattr(database, "_insert", fail_before_private_mutation)
    monkeypatch.setattr(transfer_retry.time, "sleep", delays.append)

    assert transfer(retry_cnt=3, timeout_increment=0.01) == 1

    assert len(attempts) == 2
    assert delays == [0.01]
    assert database.target.rows == [{"payload": ipaddress.ip_network(value)}]
    assert database.bound_rows == [(value,), (value,)]
    assert len(database.reads) == 1


@pytest.mark.parametrize(
    ("stage_type", "destination", "value", "expected"),
    [
        ("CIDR", "CIDR", "192.0.2.0/24", ipaddress.ip_network("192.0.2.0/24")),
        ("TEXT", SHORT_DOMAIN, "abcd", "abc"),
    ],
    ids=["native-assignment", "actual-final-cast"],
)
def test_gp_native_transfer_metadata_describes_only_the_certified_assignment(
    monkeypatch, stage_type, destination, value, expected
):
    database = install_native_transfer(
        monkeypatch, rows=[(value,)], target_schema={"payload": destination}
    )

    result = transfer(table_schema={"payload": stage_type}, return_metadata=True)

    assert result.rows == 1
    assert database.target.rows == [{"payload": expected}]
    assert result.metadata.input_count == result.metadata.source_rows == 1
    assert result.metadata.inserted_rows == result.metadata.affected_rows == 1
    assert len(result.metadata.type_resolutions) == 1
    resolution = result.metadata.type_resolutions[0]
    assert resolution["destination_type"] == stage_type
    assert resolution["native_assignment_destination_type"] == destination
    assert resolution["native_assignment_validation"] == "certified"
    assert resolution["native_assignment_required"] is False
    assert resolution["reason"] == "native_assignment_checkpoint"
    assert resolution["certified_input_count"] == 1
    assert value not in str(result.metadata.as_dict())


def test_gp_native_transfer_final_checkpoint_population_retry_reuses_certified_raw_input(
    monkeypatch,
):
    database = install_native_transfer(
        monkeypatch, rows=[("abcd",)], target_schema={"payload": SHORT_DOMAIN}
    )
    database.fail_secondary_ack = 1

    assert transfer(table_schema={"payload": "TEXT"}, full_retry_cnt=2) == 1

    assert database.target.rows == [{"payload": "abc"}]
    assert database.bound_rows == [("abcd",)]
    assert database.source_unavailable
    assert len(database.reads) == database.source_opens == 1
    assert (
        len(
            [
                identity
                for identity, _rows in database.assignment_values
                if identity != ("sandbox", "events")
            ]
        )
        == 2
    )
    assert len(database.live_mutations) == 2
    assert set(database.tables) == {("sandbox", "events")}


def test_gp_native_transfer_equal_domain_names_on_different_aliases_require_assignment(
    monkeypatch, write_sql_connections
):
    connections = deepcopy(DEFAULT_SQL_CONNECTIONS)
    connections["gp_source"] = {**connections["gp"], "database": "wider_source_domain"}
    write_sql_connections(connections)
    # The source alias's identically named domain permits twenty characters;
    # the destination domain permits three. Names cannot prove identical domains.
    value = "abcd"
    assert len(value) <= 20
    database = install_native_transfer(
        monkeypatch,
        rows=[(value,)],
        target_schema={"payload": SHORT_DOMAIN},
        source_types={"payload": SHORT_DOMAIN},
        exists=False,
        source_key="gp_source",
        source_backend="gp",
    )

    with pytest.raises(NativeAssignmentError, match="native assignment"):
        transfer(from_db="gp_source", table_schema={"payload": SHORT_DOMAIN})

    assert database.bound_rows == [(value,)]
    assert len(database.reads) == database.source_opens == 1
    assert database.live_mutations == []
    assert database.tables == {}


@pytest.mark.parametrize("flag", ["dry_run", "return_sql"])
def test_gp_native_transfer_dry_plan_reports_pending_assignment_without_connections(
    monkeypatch, flag
):
    database = install_native_transfer(
        monkeypatch, rows=[("192.0.2.0/24",)], target_schema={"payload": "CIDR"}
    )

    plan = transfer(table_schema={"payload": "CIDR"}, **{flag: True})

    assert plan.options["native_assignment_validation_pending"] is True
    assert database.opened == []
    assert database.reads == database.bound_rows == database.live_mutations == []
