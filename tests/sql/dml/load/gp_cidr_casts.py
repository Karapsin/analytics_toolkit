"""Authoritative CIDR strings retain Greenplum's native network conversion."""

from __future__ import annotations

import ipaddress
from copy import deepcopy

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.execution.plans import SqlPlan
from psycopg2.extensions import AsIs, ISQLQuote, QuotedString, adapters
from sqlglot import exp, parse_one

from tests.sql._support.gp_cidr_casts import SHORT_DOMAIN, native_cidr_target


def _load(frame, **options):
    return sql.load_df(
        "gp",
        "sandbox.events",
        frame,
        write_mode=options.pop("write_mode", "truncate_insert"),
        retry_cnt=options.pop("retry_cnt", 1),
        timeout_increment=0,
        **options,
    )


class DriverCidr:
    def __init__(self, network):
        self.network = network


def _register_native_driver_adapter(monkeypatch):
    calls = []

    def adapt(value):
        calls.append(value)
        return AsIs("NULL") if value.network is None else QuotedString(value.network)

    monkeypatch.setitem(adapters, (DriverCidr, ISQLQuote), adapt)
    return calls


def _insert_tables(database):
    trees = [
        parse_one(database._ddl_schema_prefix(sql), read="postgres") for sql in database.commands
    ]
    return [database.identity(tree.this) for tree in trees if isinstance(tree, exp.Insert)]


@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
def test_gp_cidr_casts_valid_string_reaches_native_execute_values(monkeypatch, explicit):
    database = native_cidr_target(monkeypatch)
    value = "192.0.2.0/24"
    frame = pd.DataFrame({"payload": pd.Series([value], dtype=object)})
    options = {"table_schema": {"payload": "CIDR"}} if explicit else {}

    assert (
        sql.load_df(
            "gp",
            "sandbox.events",
            frame,
            write_mode="truncate_insert",
            retry_cnt=1,
            timeout_increment=0,
            **options,
        )
        == 1
    )

    assert database.target.rows == [{"payload": ipaddress.ip_network(value)}]
    assert database.bound_rows == [(value,)]
    assert database.wire_rows == [("'192.0.2.0/24'",)]
    assert database.target.schema == {"payload": "CIDR"}
    assert len(database.live_mutations) == 2
    assert frame.iloc[0, 0] is value


@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
def test_gp_cidr_casts_nullable_ipv6_assignment_keeps_exact_driver_values(monkeypatch, explicit):
    database = native_cidr_target(monkeypatch)
    values = [None, "192.0.2.0/24", "2001:db8::/32", None]
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})
    original = frame.copy(deep=True)
    options = {"table_schema": {"payload": "CIDR"}} if explicit else {}

    assert _load(frame, **options) == len(values)

    assert database.target.rows == [
        {"payload": ipaddress.ip_network(value) if value is not None else None} for value in values
    ]
    assert database.bound_rows == [(value,) for value in values]
    assert database.target.schema == {"payload": "CIDR"}
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
def test_gp_cidr_casts_server_rejects_last_row_before_live_clear(monkeypatch, explicit):
    database = native_cidr_target(monkeypatch)
    seed = deepcopy(database.target.rows)
    frame = pd.DataFrame({"payload": ["192.0.2.0/24", "192.0.2.1/24"]})
    options = {"table_schema": {"payload": "CIDR"}} if explicit else {}

    with pytest.raises(ValueError, match=r"assignment|native|certif|host bits"):
        _load(frame, **options)

    assert database.target.rows == seed
    assert database.live_mutations == []
    assert ("sandbox", "events") not in _insert_tables(database)
    assert set(database.tables) == {("sandbox", "events")}


@pytest.mark.parametrize("exists", [False, True], ids=["new", "existing"])
def test_gp_cidr_casts_domain_assignment_rejects_before_live_create_or_clear(monkeypatch, exists):
    database = native_cidr_target(monkeypatch, SHORT_DOMAIN, exists=exists)
    seed = deepcopy(database.tables)
    frame = pd.DataFrame({"payload": ["ok", "abcd"]})

    with pytest.raises(ValueError, match=r"assignment|native|certif|varying"):
        _load(frame, table_schema={"payload": SHORT_DOMAIN})

    assert database.live_mutations == []
    assert ("sandbox", "events") not in _insert_tables(database)
    assert set(database.tables) == set(seed)
    if exists:
        assert database.target.rows == seed[("sandbox", "events")].rows


@pytest.mark.parametrize("lose_ack", [False, True], ids=["publication", "lost-target-ack"])
def test_gp_cidr_casts_registered_driver_adapts_once_for_checkpoint_and_replay(
    monkeypatch, lose_ack
):
    database = native_cidr_target(monkeypatch)
    database.lost_target_acknowledgements = int(lose_ack)
    calls = _register_native_driver_adapter(monkeypatch)
    value = DriverCidr("192.0.2.0/24")
    frame = pd.DataFrame({"payload": pd.Series([value], dtype=object)})

    assert _load(frame, retry_cnt=2 if lose_ack else 1) == 1

    assert calls == [value]
    assert database.bound_rows == [(value,)]
    insert_tables = _insert_tables(database)
    target_inserts = insert_tables.count(("sandbox", "events"))
    assert target_inserts == (2 if lose_ack else 1)
    assert len([table for table in insert_tables if table != ("sandbox", "events")]) == 1
    assert 1 <= len(database.target.rows) <= target_inserts
    assert all(
        row == {"payload": ipaddress.ip_network(value.network)} for row in database.target.rows
    )
    assert set(database.tables) == {("sandbox", "events")}
    assert frame.iloc[0, 0] is value
    assert value.network == "192.0.2.0/24"


def test_gp_cidr_casts_native_key_duplicates_reject_on_typed_stage_before_live_write(monkeypatch):
    database = native_cidr_target(monkeypatch, empty=True)
    values = ["2001:0db8::/32", "2001:db8::/32"]
    frame = pd.DataFrame({"payload": values})
    assert not frame.duplicated().any()

    with pytest.raises(ValueError, match=r"Duplicate.*key"):
        _load(frame, write_mode="append", key_columns="payload")

    assert database.target.rows == []
    assert database.live_mutations == []
    assert database.bound_rows == [(value,) for value in values]
    assert set(database.tables) == {("sandbox", "events")}


def test_gp_cidr_casts_driver_null_key_rejects_on_typed_stage_before_live_write(monkeypatch):
    database = native_cidr_target(monkeypatch, empty=True)
    calls = _register_native_driver_adapter(monkeypatch)
    values = [DriverCidr("192.0.2.0/24"), DriverCidr(None)]
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})
    assert not frame["payload"].isna().any()

    with pytest.raises(ValueError, match=r"key.*SQL NULL"):
        _load(frame, write_mode="append", key_columns="payload")

    assert calls == values
    assert database.target.rows == []
    assert database.live_mutations == []
    assert set(database.tables) == {("sandbox", "events")}
    assert all(frame.iloc[index, 0] is value for index, value in enumerate(values))
    assert values[1].network is None


@pytest.mark.parametrize("flag", ["dry_run", "return_sql"])
def test_gp_cidr_casts_offline_plans_do_not_query_or_create_stages(monkeypatch, flag):
    database = native_cidr_target(monkeypatch, exists=False)
    frame = pd.DataFrame({"payload": ["192.0.2.0/24"]})

    plan = _load(frame, table_schema={"payload": "CIDR"}, **{flag: True})

    assert isinstance(plan, SqlPlan)
    assert plan.options["native_assignment_validation_pending"] is True
    assert database.opened == []
    assert database.commands == []
    assert database.tables == {}


def test_gp_cidr_casts_known_integer_keeps_strict_local_fast_path(monkeypatch):
    database = native_cidr_target(monkeypatch, "INTEGER")
    frame = pd.DataFrame({"payload": pd.Series([42], dtype=object)})

    assert _load(frame) == 1

    assert database.target.rows == [{"payload": 42}]
    assert database.bound_rows == [(42,)]
    assert _insert_tables(database) == [("sandbox", "events")]


@pytest.mark.parametrize("invalid", [False, True], ids=["valid-array", "invalid-last-leaf"])
def test_gp_cidr_casts_unknown_array_leaves_use_native_assignment_before_clear(
    monkeypatch, invalid
):
    database = native_cidr_target(monkeypatch, "CIDR[]")
    seed = deepcopy(database.target.rows)
    value = ["192.0.2.0/24", None, "192.0.2.1/24" if invalid else "2001:db8::/32"]
    frame = pd.DataFrame({"payload": pd.Series([value], dtype=object)})
    original = deepcopy(value)

    if invalid:
        with pytest.raises(ValueError, match=r"assignment|native|certif|host bits"):
            _load(frame)
        assert database.target.rows == seed
        assert database.live_mutations == []
    else:
        assert _load(frame) == 1
        assert database.target.rows == [
            {
                "payload": [
                    ipaddress.ip_network(item) if item is not None else None for item in value
                ]
            }
        ]
        assert database.bound_rows == [(value,)]
    assert set(database.tables) == {("sandbox", "events")}
    assert frame.iloc[0, 0] is value
    assert value == original


def test_gp_cidr_casts_real_driver_adaptation_failure_preserves_live_seed(monkeypatch):
    database = native_cidr_target(monkeypatch)
    seed = deepcopy(database.target.rows)
    calls = _register_native_driver_adapter(monkeypatch)
    value = DriverCidr(object())
    frame = pd.DataFrame({"payload": pd.Series([value], dtype=object)})

    with pytest.raises((TypeError, ValueError), match=r"adapt|quote|native|certif"):
        _load(frame)

    assert calls == [value]
    assert database.target.rows == seed
    assert database.live_mutations == []
    assert set(database.tables) == {("sandbox", "events")}
    assert frame.iloc[0, 0] is value


def test_gp_cidr_casts_domain_array_last_element_cannot_truncate_before_assignment(monkeypatch):
    database = native_cidr_target(monkeypatch, SHORT_DOMAIN + "[]")
    seed = deepcopy(database.target.rows)
    value = ["ok", None, "long"]
    frame = pd.DataFrame({"payload": pd.Series([value], dtype=object)})

    def load_domain_array():
        assert _load(frame) == 1
        assert database.target.rows == [{"payload": ["ok", None, "lon"]}]
        pytest.fail(
            "Native domain-array bind silently truncated the final element before assignment."
        )

    with pytest.raises(ValueError, match=r"assignment|native|certif|varying"):
        load_domain_array()

    assert database.target.rows == seed
    assert database.live_mutations == []
    assert set(database.tables) == {("sandbox", "events")}
    assert frame.iloc[0, 0] is value
    assert value == ["ok", None, "long"]


def test_gp_cidr_casts_valid_domain_array_preserves_null_and_empty_values(monkeypatch):
    database = native_cidr_target(monkeypatch, SHORT_DOMAIN + "[]")
    values = [["ok", None, "a"], None, []]
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})
    original = deepcopy(values)

    assert _load(frame) == len(values)

    assert database.target.rows == [{"payload": value} for value in values]
    assert database.bound_rows == [(value,) for value in values]
    assert database.target.schema == {"payload": SHORT_DOMAIN + "[]"}
    assert set(database.tables) == {("sandbox", "events")}
    assert frame.iloc[0, 0] is values[0]
    assert frame.iloc[2, 0] is values[2]
    assert values == original


@pytest.mark.parametrize("transient", [False, True], ids=["compatible", "second-stage-retry"])
def test_gp_cidr_casts_explicit_and_final_authorities_use_native_assignment_without_readaptation(
    monkeypatch, transient
):
    database = native_cidr_target(monkeypatch, "INET")
    database.fail_private_assignments = int(transient)
    calls = _register_native_driver_adapter(monkeypatch)
    raw = "192.0.2.0/24"
    value = DriverCidr(raw) if transient else raw
    frame = pd.DataFrame({"payload": pd.Series([value], dtype=object)})

    assert _load(frame, table_schema={"payload": "CIDR"}, retry_cnt=2 if transient else 1) == 1

    assert database.target.schema == {"payload": "INET"}
    assert database.target.rows == [{"payload": ipaddress.ip_interface(raw)}]
    assert database.bound_rows == [(value,)]
    assert calls == ([value] if transient else [])
    assert database.private_assignment_attempts == (2 if transient else 1)
    assert all(tree.find(exp.Cast) is None for tree in database.assignment_selects)
    assert _insert_tables(database).count(("sandbox", "events")) == 1
    assert set(database.tables) == {("sandbox", "events")}
    assert frame.iloc[0, 0] is value


def test_gp_cidr_casts_incompatible_final_authority_rejects_before_live_clear(monkeypatch):
    database = native_cidr_target(monkeypatch, SHORT_DOMAIN)
    seed = deepcopy(database.target.rows)
    value = "192.0.2.0/24"
    frame = pd.DataFrame({"payload": pd.Series([value], dtype=object)})

    with pytest.raises(ValueError, match=r"assignment|native|certif|varying"):
        _load(frame, table_schema={"payload": "CIDR"})

    assert database.target.schema == {"payload": SHORT_DOMAIN}
    assert database.target.rows == seed
    assert database.live_mutations == []
    assert database.bound_rows == [(value,)]
    assert database.private_assignment_attempts == 1
    assert ("sandbox", "events") not in _insert_tables(database)
    assert set(database.tables) == {("sandbox", "events")}
    assert frame.iloc[0, 0] is value
