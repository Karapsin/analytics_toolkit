from __future__ import annotations

import datetime as dt
from dataclasses import replace
from types import SimpleNamespace

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.core.exact_values import ExactTemporal
from analytics_toolkit.sql.dml.transfer.flow import finalize

from tests.sql._support.adaptive_transfer import (
    Connection,
    install,
    require_streaming_fast_path,
    track_spools,
)
from tests.sql._support.load_table import load_df_module
from tests.sql.dml.transfer.schema.temporal_calendar import patch_load_boundaries

_BAD_POLICIES = [
    ("Europe/Moscow", "DATE", "DateTime", dt.date(1970, 1, 2), dt.date(1970, 1, 1)),
    (
        "America/Los_Angeles",
        "TIMESTAMP(6)",
        "DateTime64(9)",
        dt.datetime.fromisoformat("2024-01-01T00:00:00"),
        dt.datetime.fromisoformat("2262-04-11T23:30:00"),
    ),
]


def transfer_policy(monkeypatch, zone, types, values, *, existing=True):
    source_type, target_type = types
    monkeypatch.setattr(Connection, "server_tz", zone)
    scenario = install(
        monkeypatch,
        native_type=source_type,
        rows=[(value,) for value in values],
        from_db_key="gp",
        from_db_backend="gp",
        to_db_key="ch",
        to_db_backend="ch",
        write_mode="append",
        replace_target_table=False,
        table_schema=None if existing else {"id": target_type},
    )
    if existing:
        scenario.existing_types = {"id": target_type}
    return scenario


def load_policy(monkeypatch, zone, schema, frame, *, write_mode="truncate_insert"):
    created, loaded = patch_load_boundaries(monkeypatch, schema)
    statements = []
    connection = SimpleNamespace(server_tz=zone, command=statements.append, close=lambda: None)
    monkeypatch.setattr(load_df_module, "get_sql_connection", lambda _key: connection)

    def call():
        return sql.load_df(
            "ch",
            "target.events",
            frame,
            write_mode=write_mode,
            table_schema=None,
            ch_distributed_table=False,
            ch_engine="MergeTree",
            order_by="id",
            retry_cnt=1,
            timeout_increment=0,
        )

    return call, created, loaded, statements


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("case", _BAD_POLICIES)
def test_public_transfer_uses_actual_target_timezone_before_timestamp_mutation(
    monkeypatch, existing, case
):
    zone, source_type, target_type, safe, bad = case
    scenario = transfer_policy(
        monkeypatch, zone, (source_type, target_type), [safe, bad], existing=existing
    )
    paths = track_spools(monkeypatch)

    with pytest.raises(ValueError, match="authoritative timestamp range"):
        scenario.run()

    assert scenario.read_count == 1
    assert "create_target" not in scenario.events
    assert not scenario.loaded
    assert scenario.finalizations == 0
    assert paths
    assert all(not path.exists() for path in paths)


@pytest.mark.parametrize("case", _BAD_POLICIES)
def test_public_load_truncate_insert_validates_implicit_timezone_before_truncate_or_ddl(
    monkeypatch, case
):
    zone, _source_type, target_type, safe, bad = case
    frame = pd.DataFrame({"id": pd.Series([safe, bad], dtype=object)})
    call, created, loaded, statements = load_policy(monkeypatch, zone, target_type, frame)

    with pytest.raises(ValueError, match="authoritative timestamp range"):
        call()

    assert not statements
    assert not created
    assert not loaded


@pytest.mark.parametrize(
    "case",
    [
        (
            "Europe/Moscow",
            "DATE",
            "DateTime('UTC')",
            dt.date(1970, 1, 1),
            "1970-01-01T00:00:00+00:00",
        ),
        (
            "America/Los_Angeles",
            "TIMESTAMP(6)",
            "DateTime64(9, 'UTC')",
            dt.datetime.fromisoformat("2262-04-11T23:30:00"),
            "2262-04-11T23:30:00+00:00",
        ),
        ("Europe/Moscow", "DATE", "DateTime", dt.date(1970, 1, 2), "1970-01-01T21:00:00+00:00"),
    ],
)
def test_public_transfer_freezes_writer_policy_without_changing_column_ddl(monkeypatch, case):
    zone, source_type, target_type, value, utc = case
    scenario = transfer_policy(monkeypatch, zone, (source_type, target_type), [value])

    assert scenario.run() == 1

    assert scenario.schemas == [{"id": target_type}]
    actual = scenario.loaded[0][0][0]
    assert actual.tzinfo is not None
    assert actual.astimezone(dt.timezone.utc) == dt.datetime.fromisoformat(utc)


def test_public_load_nested_implicit_timestamp_requires_the_same_target_zone(monkeypatch):
    bad = dt.datetime.fromisoformat("2262-04-11T23:30:00")
    frame = pd.DataFrame({"id": pd.Series([[bad]], dtype=object)})
    call, created, loaded, statements = load_policy(
        monkeypatch, "America/Los_Angeles", "Array(DateTime64(9))", frame
    )

    with pytest.raises(ValueError, match="authoritative timestamp range"):
        call()

    assert not statements
    assert not created
    assert not loaded


def test_certified_transfer_cannot_rebind_to_a_different_timezone_during_retry(monkeypatch):
    scenario = transfer_policy(
        monkeypatch, "Europe/Moscow", ("DATE", "DateTime"), [dt.date(1970, 1, 2)]
    )
    scenario.options = replace(scenario.options, replace_target_table=True)
    original = finalize.finalize_loaded_stage
    attempts = []

    def fail(*args, **kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            monkeypatch.setattr(Connection, "server_tz", "America/Los_Angeles")
            message = "target acknowledgement lost"
            raise ConnectionError(message)
        return original(*args, **kwargs)

    monkeypatch.setattr(finalize, "finalize_loaded_stage", fail)
    with pytest.raises(InvalidSqlInputError, match="timezone differs"):
        scenario.run()

    assert scenario.read_count == 1
    assert len(scenario.loaded) == 1
    assert scenario.events.count("create_target") == 1


def test_load_cannot_change_zone_between_metadata_and_truncation(monkeypatch):
    frame = pd.DataFrame({"id": [dt.date(1970, 1, 2)]})
    call, created, loaded, statements = load_policy(monkeypatch, "Europe/Moscow", "DateTime", frame)
    zones = iter(["Europe/Moscow", "America/Los_Angeles"])
    monkeypatch.setattr(
        load_df_module,
        "get_sql_connection",
        lambda _key: SimpleNamespace(
            server_tz=next(zones), command=statements.append, close=lambda: None
        ),
    )

    with pytest.raises(InvalidSqlInputError, match="timezone differs"):
        call()

    assert not statements
    assert not created
    assert not loaded


def test_load_unknown_implicit_zone_fails_closed_before_truncation(monkeypatch):
    frame = pd.DataFrame({"id": [dt.date(1970, 1, 2)]})
    call, created, loaded, statements = load_policy(monkeypatch, None, "DateTime", frame)

    with pytest.raises(InvalidSqlInputError, match="Cannot resolve the destination"):
        call()

    assert not statements
    assert not created
    assert not loaded


def test_load_lazy_handshake_resolves_zone_before_calendar_validation(monkeypatch):
    frame = pd.DataFrame({"id": [dt.date(1970, 1, 1)]})
    call, created, loaded, statements = load_policy(monkeypatch, None, "DateTime", frame)
    connection = SimpleNamespace(server_tz=None, command=statements.append, close=lambda: None)
    probes = []

    def query(statement):
        probes.append(statement)
        connection.server_tz = "Europe/Moscow"
        return SimpleNamespace(result_rows=[("Europe/Moscow",)])

    connection.query = query
    monkeypatch.setattr(load_df_module, "get_sql_connection", lambda _key: connection)
    with pytest.raises(ValueError, match="authoritative timestamp range"):
        call()

    assert len(probes) == 1
    assert "SELECT timezone()" in probes[0]
    assert not statements
    assert not created
    assert not loaded


@pytest.mark.parametrize("stamp", ["2024-11-03T01:30:00", "2024-03-10T02:30:00"])
def test_load_rejects_ambiguous_or_missing_naive_wallclock_before_truncate(monkeypatch, stamp):
    frame = pd.DataFrame({"id": [dt.datetime.fromisoformat(stamp)]})
    call, created, loaded, statements = load_policy(
        monkeypatch, "America/Los_Angeles", "DateTime64(6)", frame
    )

    with pytest.raises(ValueError, match=r"ambiguous|nonexistent"):
        call()

    assert not statements
    assert not created
    assert not loaded


def test_transfer_source_read_zone_binds_the_instant_before_destination_encoding(monkeypatch):
    monkeypatch.setattr(Connection, "effective_read_timezone", "Europe/Moscow")
    monkeypatch.setattr(Connection, "server_tz", "UTC")
    value = dt.datetime.fromisoformat("1970-01-01T03:00:00")
    scenario = install(
        monkeypatch,
        native_type="DateTime",
        rows=[(value,)],
        to_db_key="ch",
        to_db_backend="ch",
        write_mode="append",
        replace_target_table=False,
    )
    scenario.existing_types = {"id": "DateTime64(6)"}

    assert scenario.run() == 1

    actual = scenario.loaded[0][0][0]
    assert actual.astimezone(dt.timezone.utc) == dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    assert scenario.schemas == [{"id": "DateTime64(6)"}]
    assert scenario.events.index("create_target") < scenario.events.index(
        "complete:SELECT id FROM source"
    )


@pytest.mark.parametrize(
    ("value", "expected_type"),
    [
        (dt.datetime.fromisoformat("2262-04-11T23:30:00"), "DateTime64(6)"),
        (pd.Timestamp("2262-04-11T23:30:00.000000001"), "String"),
    ],
)
def test_new_target_resolves_implicit_zone_range_before_publishing(
    monkeypatch, value, expected_type
):
    monkeypatch.setattr(Connection, "server_tz", "America/Los_Angeles")
    scenario = install(
        monkeypatch,
        native_type="TIMESTAMP(9)",
        rows=[(value,)],
        from_db_key="gp",
        from_db_backend="gp",
        to_db_key="ch",
        to_db_backend="ch",
        write_mode="append",
        replace_target_table=False,
    )

    assert scenario.run() == 1

    assert scenario.schemas == [{"id": expected_type}]
    assert scenario.read_count == 1
    assert scenario.events.index("complete:SELECT id FROM source") < scenario.events.index(
        "create_target"
    )
    actual = scenario.loaded[0][0][0]
    if expected_type == "String":
        assert actual == "2262-04-11T23:30:00.000000001"
    else:
        assert actual.astimezone(dt.timezone.utc).isoformat() == "2262-04-12T06:30:00+00:00"


def test_transfer_preserves_both_source_dst_fold_instants_on_streaming_path(monkeypatch):
    ticks = [1730622600, 1730626200]
    values = [ExactTemporal(tick, 0, "UTC", offset_naive=False) for tick in ticks]
    monkeypatch.setattr(Connection, "effective_read_timezone", "UTC")
    monkeypatch.setattr(Connection, "server_tz", "America/Los_Angeles")
    scenario = install(
        monkeypatch,
        native_type="DateTime",
        rows=[(value,) for value in values],
        to_db_key="ch",
        to_db_backend="ch",
        write_mode="append",
        replace_target_table=False,
    )
    scenario.existing_types = {"id": "DateTime64(6)"}
    require_streaming_fast_path(monkeypatch)

    assert scenario.run() == 2

    assert [row[0].ticks for row in scenario.loaded[0]] == ticks
    assert scenario.read_count == 1
    assert scenario.events.index("create_target") < scenario.events.index(
        "complete:SELECT id FROM source"
    )


@pytest.mark.parametrize(
    ("value", "expected_type"),
    [
        (dt.datetime.fromisoformat("2262-04-11T23:30:00"), "DateTime64(6)"),
        (pd.Timestamp("2262-04-11T23:30:00.000000001"), "String"),
    ],
)
def test_new_load_resolves_implicit_zone_before_creating_native_or_text_column(
    monkeypatch, value, expected_type
):
    frame = pd.DataFrame({"id": pd.Series([value], dtype=object)})
    call, created, loaded, statements = load_policy(
        monkeypatch, "America/Los_Angeles", None, frame, write_mode="append"
    )

    assert call() == 1

    assert created == [{"id": expected_type}]
    assert not statements
    actual = loaded[0][0]
    if expected_type == "String":
        assert actual == "2262-04-11T23:30:00.000000001"
    else:
        assert actual.astimezone(dt.timezone.utc).isoformat() == "2262-04-12T06:30:00+00:00"


@pytest.mark.parametrize("stamp", ["2024-11-03T01:30:00", "2024-03-10T02:30:00"])
def test_inferred_transfer_preserves_unresolved_dst_wallclock_as_text(monkeypatch, stamp):
    monkeypatch.setattr(Connection, "server_tz", "America/Los_Angeles")
    value = dt.datetime.fromisoformat(stamp)
    scenario = install(
        monkeypatch,
        native_type="TIMESTAMP(6)",
        rows=[(value,)],
        from_db_key="gp",
        from_db_backend="gp",
        to_db_key="ch",
        to_db_backend="ch",
        write_mode="append",
        replace_target_table=False,
    )

    assert scenario.run() == 1

    assert scenario.schemas == [{"id": "String"}]
    assert scenario.loaded == [[(stamp + ".000000",)]]
    assert scenario.read_count == 1
    assert scenario.events.index("complete:SELECT id FROM source") < scenario.events.index(
        "create_target"
    )


@pytest.mark.parametrize(
    "values",
    [
        [dt.datetime.fromisoformat("2024-11-03T01:30:00")],
        [dt.datetime.fromisoformat("2024-03-10T02:30:00")],
        [
            dt.datetime.fromisoformat("2024-01-01T12:00:00"),
            dt.datetime.fromisoformat("2024-01-01T12:00:00+00:00"),
        ],
    ],
)
def test_inferred_load_uses_text_without_inventing_dst_or_mixed_awareness_policy(
    monkeypatch, values
):
    frame = pd.DataFrame({"id": pd.Series(values, dtype=object)})
    call, created, loaded, statements = load_policy(
        monkeypatch, "America/Los_Angeles", None, frame, write_mode="append"
    )

    assert call() == len(values)

    assert created == [{"id": "String"}]
    assert loaded == [[value.isoformat() for value in values]]
    assert not statements


def test_inferred_transfer_does_not_resolve_mixed_awareness_with_server_default(monkeypatch):
    monkeypatch.setattr(Connection, "server_tz", "America/Los_Angeles")
    values = [
        dt.datetime.fromisoformat("2024-01-01T12:00:00"),
        dt.datetime.fromisoformat("2024-01-01T12:00:00+00:00"),
    ]
    scenario = install(
        monkeypatch,
        native_type="TIMESTAMP(6)",
        rows=[(value,) for value in values],
        from_db_key="gp",
        from_db_backend="gp",
        to_db_key="ch",
        to_db_backend="ch",
        write_mode="append",
        replace_target_table=False,
    )

    assert scenario.run() == 2

    assert scenario.schemas == [{"id": "String"}]
    assert scenario.loaded == [
        [("2024-01-01T12:00:00.000000",), ("2024-01-01T12:00:00.000000+00:00",)]
    ]
    assert scenario.read_count == 1


@pytest.mark.parametrize(
    ("backend", "expected_type"),
    [("gp", "TIMESTAMP WITH TIME ZONE"), ("trino", "TIMESTAMP(6) WITH TIME ZONE")],
)
def test_inferred_cross_backend_ch_timestamps_preserve_physical_instants(
    monkeypatch, backend, expected_type
):
    values = [
        ExactTemporal(0, 0, "UTC", offset_naive=False),
        ExactTemporal(1730626200, 0, "UTC", offset_naive=False),
    ]
    scenario = install(
        monkeypatch,
        native_type="DateTime",
        rows=[(value,) for value in values],
        to_db_key=backend,
        to_db_backend=backend,
    )
    require_streaming_fast_path(monkeypatch)

    assert scenario.run() == len(values)

    assert scenario.schemas == [{"id": expected_type}]
    assert scenario.source_schema[0].native_type == "DateTime"
    assert [row[0].ticks for row in scenario.loaded[0]] == [0, 1730626200]
    assert scenario.read_count == 1
