"""Raw SDK transfer rows retain precision through full-input type resolution."""

from __future__ import annotations

import json

import pandas as pd
import pytest
from analytics_toolkit.sql.core.exact_values import ExactTemporal, decode_value

from tests.sql._support import adaptive_transfer as transfer_support
from tests.sql._support import exact_trino_transfer as raw_transfer_support
from tests.sql._support.adaptive_transfer import TransferSlice, track_spools
from tests.sql._support.exact_trino_transfer import RawTransferSource, install_raw_trino_transfer
from tests.sql._support.exact_trino_values import raw_type, timestamp_type

QUERY = "SELECT stamp FROM source"


def _ticks12(value):
    if isinstance(value, ExactTemporal):
        return value.ticks * 10 ** (12 - value.scale)
    return pd.Timestamp(value).value * 1000


def _source(precision, rows, *, aware=False):
    native = f"TIMESTAMP({precision})" + (" WITH TIME ZONE" if aware else "")
    return RawTransferSource(native, timestamp_type(precision, aware=aware), {QUERY: rows})


@pytest.mark.parametrize("aware", [False, True], ids=["naive", "aware"])
def test_exact_trino_transfer_aligned_nanoseconds_keep_native_microseconds(
    monkeypatch, write_sql_connections, aware
):
    suffix = " UTC" if aware else ""
    source = _source(
        9,
        (
            [[f"1970-01-01 00:00:01.123456000{suffix}"]],
            [[f"1970-01-01 00:00:01.123457000{suffix}"]],
        ),
        aware=aware,
    )
    scenario = install_raw_trino_transfer(monkeypatch, write_sql_connections, source)

    result = scenario.run(metadata=True)

    assert result.rows == 2
    target = "TIMESTAMP(6)" + (" WITH TIME ZONE" if aware else "")
    assert scenario.schemas == [{"stamp": target}]
    assert [_ticks12(row[0]) for row in scenario.loaded[0]] == [1_123456000000, 1_123457000000]
    assert source.calls == ["POST", "GET"]
    assert len(source.statements) == 1
    assert scenario.read_count == 1
    assert scenario.events.index(f"complete:{QUERY}") < scenario.events.index("create_target")
    assert result.metadata.input_count == 2


@pytest.mark.parametrize("aware", [False, True], ids=["naive", "aware"])
def test_exact_trino_transfer_late_nanoseconds_choose_whole_column_text(
    monkeypatch, write_sql_connections, aware
):
    suffix = " UTC" if aware else ""
    source = _source(
        9,
        (
            [[f"1970-01-01 00:00:01.123456000{suffix}"]],
            [[f"1970-01-01 00:00:01.123456789{suffix}"]],
        ),
        aware=aware,
    )
    scenario = install_raw_trino_transfer(monkeypatch, write_sql_connections, source)

    result = scenario.run(metadata=True)

    assert result.rows == 2
    assert scenario.schemas == [{"stamp": "VARCHAR"}]
    target_suffix = "+00:00" if aware else ""
    assert scenario.loaded == [
        [
            (f"1970-01-01T00:00:01.123456000{target_suffix}",),
            (f"1970-01-01T00:00:01.123456789{target_suffix}",),
        ]
    ]
    assert source.calls == ["POST", "GET"]
    assert len(source.statements) == 1
    assert scenario.read_count == 1
    assert scenario.events.index(f"complete:{QUERY}") < scenario.events.index("create_target")
    assert result.metadata.input_count == 2


def test_exact_trino_transfer_profiles_every_key_before_native_schema_publication(
    monkeypatch, write_sql_connections
):
    first_query = "SELECT stamp FROM first_slice"
    last_query = "SELECT stamp FROM last_slice"
    source = RawTransferSource(
        "TIMESTAMP(9)",
        timestamp_type(),
        {
            first_query: ([["1970-01-01 00:00:01.123456000"]],),
            last_query: ([["1970-01-01 00:00:01.123456789"]],),
        },
    )
    scenario = install_raw_trino_transfer(
        monkeypatch,
        write_sql_connections,
        source,
        transfer_slices=[
            TransferSlice(0, (1,), "", first_query, None),
            TransferSlice(1, (2,), "", last_query, None),
        ],
    )

    result = scenario.run(metadata=True)

    assert result.rows == 2
    assert scenario.schemas == [{"stamp": "VARCHAR"}]
    assert scenario.loaded == [
        [("1970-01-01T00:00:01.123456000",), ("1970-01-01T00:00:01.123456789",)]
    ]
    assert source.calls == ["POST", "POST"]
    assert len(source.statements) == 2
    assert all(
        sum(query in statement for statement in source.statements) == 1 for query in source.queries
    )
    assert scenario.read_count == 2
    assert scenario.events.index(f"complete:{last_query}") < scenario.events.index("create_target")
    assert result.metadata.input_count == 2


def test_exact_trino_transfer_retry_replays_certified_pages_without_requery(
    monkeypatch, write_sql_connections
):
    source = _source(9, ([["1970-01-01 00:00:01.123456000"]], [["1970-01-01 00:00:01.123456789"]]))
    scenario = install_raw_trino_transfer(monkeypatch, write_sql_connections, source)
    scenario.fail_finalization_once = True
    scenario.make_source_unavailable_on_retry = True

    assert scenario.run() == 2

    expected = [("1970-01-01T00:00:01.123456000",), ("1970-01-01T00:00:01.123456789",)]
    assert scenario.loaded == [expected, expected]
    assert scenario.finalizations == 2
    assert scenario.source_open_count == 1
    assert scenario.read_count == 1
    assert source.calls == ["POST", "GET"]
    assert len(source.statements) == 1


@pytest.mark.parametrize("explicit", [False, True], ids=["existing-strict", "explicit-floor"])
@pytest.mark.parametrize("aware", [False, True], ids=["naive", "aware"])
def test_exact_trino_transfer_microsecond_authority_retains_requested_conversion_policy(
    monkeypatch, write_sql_connections, explicit, aware
):
    suffix = " UTC" if aware else ""
    source = _source(9, ([[f"1970-01-01 00:00:01.123456789{suffix}"]],), aware=aware)
    target = "TIMESTAMP(6)" + (" WITH TIME ZONE" if aware else "")
    scenario = install_raw_trino_transfer(
        monkeypatch,
        write_sql_connections,
        source,
        write_mode="truncate_insert",
        table_schema={"stamp": target} if explicit else None,
    )
    scenario.existing_types = {"stamp": target}
    if explicit:
        assert scenario.run() == 1
        assert scenario.schemas == [{"stamp": target}]
        assert [_ticks12(row[0]) for row in scenario.loaded[0]] == [1_123456000000]
    else:
        with pytest.raises(ValueError, match=r"precision|timestamp"):
            scenario.run()
        assert scenario.loaded == []
        assert scenario.schemas == []
        assert "create_target" not in scenario.events
        assert scenario.finalizations == 0
    assert scenario.existing_types == {"stamp": target}
    assert source.calls == ["POST"]
    assert len(source.statements) == 1


@pytest.mark.parametrize("precision", [9, 12])
@pytest.mark.parametrize("aware", [False, True], ids=["naive", "aware"])
def test_exact_trino_transfer_configured_native_precision_keeps_every_tick(
    monkeypatch, write_sql_connections, precision, aware
):
    suffix = " UTC" if aware else ""
    fractions = ("123456789", "123456790") if precision == 9 else ("123456789001", "123456789002")
    source = _source(
        precision,
        ([[f"1970-01-01 00:00:01.{fraction}{suffix}"] for fraction in fractions],),
        aware=aware,
    )
    target = source.native_type
    scenario = install_raw_trino_transfer(
        monkeypatch,
        write_sql_connections,
        source,
        write_mode="append",
        table_schema={"stamp": target},
    )
    scenario.existing_types = {"stamp": target}

    assert scenario.run() == 2

    expected = (
        [1_123456789000, 1_123456790000] if precision == 9 else [1_123456789001, 1_123456789002]
    )
    assert [_ticks12(row[0]) for row in scenario.loaded[0]] == expected
    assert scenario.schemas == [{"stamp": target}]
    assert scenario.existing_types == {"stamp": target}
    assert source.calls == ["POST"]
    assert len(source.statements) == 1


def test_exact_trino_transfer_nested_map_keeps_every_nanosecond_key(
    monkeypatch, write_sql_connections
):
    native = "MAP(TIMESTAMP(9), VARCHAR)"
    signature = raw_type(
        "map",
        {"kind": "TYPE", "value": timestamp_type()},
        {"kind": "TYPE", "value": raw_type("varchar")},
    )
    source = RawTransferSource(
        native,
        signature,
        {
            QUERY: (
                [
                    [
                        {
                            "1970-01-01 00:00:01.123456001": "first",
                            "1970-01-01 00:00:01.123456002": "second",
                        }
                    ]
                ],
            )
        },
    )
    scenario = install_raw_trino_transfer(monkeypatch, write_sql_connections, source)

    assert scenario.run() == 1

    value = scenario.loaded[0][0][0]
    if isinstance(value, str):
        envelope = json.loads(value)
        assert envelope["backend"] == "trino"
        assert envelope["type"] == native
        value = decode_value(envelope["value"])
    assert len(value) == 2
    assert {_ticks12(key): item for key, item in value.items()} == {
        1_123456001000: "first",
        1_123456002000: "second",
    }
    assert "TIMESTAMP(9)" not in scenario.schemas[0]["stamp"]
    assert source.calls == ["POST"]
    assert len(source.statements) == 1


@pytest.mark.parametrize("aware", [False, True], ids=["naive", "fixed-offset"])
def test_exact_trino_transfer_configured_clock_precision_keeps_date_free_ticks(
    monkeypatch, write_sql_connections, aware
):
    native = "TIME(9)" + (" WITH TIME ZONE" if aware else "")
    signature = raw_type("time with time zone" if aware else "time", {"kind": "LONG", "value": 9})
    suffix = " +03:00" if aware else ""
    source = RawTransferSource(native, signature, {QUERY: ([[f"12:30:45.123456789{suffix}"]],)})
    scenario = install_raw_trino_transfer(
        monkeypatch,
        write_sql_connections,
        source,
        write_mode="append",
        table_schema={"stamp": native},
    )
    scenario.existing_types = {"stamp": native}

    assert scenario.run() == 1

    assert scenario.schemas == [{"stamp": native}]
    decoded = scenario.loaded[0][0][0]
    assert decoded.ticks == 45_045_123_456_789
    assert decoded.scale == 9
    assert decoded.offset_minutes == (180 if aware else None)
    assert scenario.existing_types == {"stamp": native}
    assert source.calls == ["POST"]
    assert len(source.statements) == 1


@pytest.mark.parametrize("write_mode", ["append", "truncate_insert"])
def test_exact_trino_transfer_ambiguous_late_wire_cannot_publish_a_partial_generation(
    monkeypatch, write_sql_connections, write_mode
):
    source = _source(
        9,
        (
            [["2024-11-03 00:30:00.123456789 UTC"]],
            [["2024-11-03 00:31:00.123456789 UTC"]],
            [["2024-11-03 01:30:00.123456789 America/New_York"]],
        ),
        aware=True,
    )
    scenario = install_raw_trino_transfer(
        monkeypatch,
        write_sql_connections,
        source,
        write_mode=write_mode,
        full_retry_cnt=1,
    )
    scenario.existing_types = {"stamp": source.native_type}
    paths = track_spools(monkeypatch)

    with pytest.raises(ValueError, match=r"ambiguous|Ambiguous|offset|fold"):
        scenario.run()

    assert scenario.loaded == []
    assert scenario.schemas == []
    assert "create_target" not in scenario.events
    assert "write_batch" not in scenario.events
    assert f"complete:{QUERY}" not in scenario.events
    assert scenario.finalizations == 0
    assert "spool" not in scenario.options.input_resolution
    assert paths
    assert all(not path.exists() for path in paths)
    assert source.calls == ["POST", "GET", "GET"]
    assert len(source.statements) == 1


@pytest.mark.parametrize("write_mode", ["append", "truncate_insert"])
def test_exact_trino_transfer_late_plain_calendar_failure_cannot_publish_partial_input(
    monkeypatch, write_sql_connections, write_mode
):
    metadata_queries = []

    class Cursor:
        def execute(self, statement, params=None):
            metadata_queries.append((statement, params))
            if "system.metadata.catalogs" in statement:
                assert params == ["iceberg"]
                self.rows = [("iceberg",)]
            elif "SHOW CREATE TABLE" in statement:
                self.rows = [
                    (
                        'CREATE TABLE "iceberg"."sandbox"."target" (stamp TIMESTAMP(9)) '
                        "WITH (format_version = 3)",
                    )
                ]
            else:
                message = f"Unexpected Iceberg timestamp metadata query: {statement}"
                raise AssertionError(message)

        def fetchall(self):
            return self.rows

        def close(self):
            pass

    class Connection(transfer_support.Connection):
        catalog = "iceberg"
        schema = "sandbox"

        def cursor(self):
            return Cursor()

    monkeypatch.setattr(transfer_support, "Connection", Connection)
    monkeypatch.setattr(raw_transfer_support, "Connection", Connection)
    source = _source(
        9,
        (
            [["1970-01-01 00:00:01.123456789"]],
            [["1970-01-01 00:00:02.123456789"]],
            [["+10000-01-01 00:00:00.123456789"]],
        ),
    )
    scenario = install_raw_trino_transfer(
        monkeypatch,
        write_sql_connections,
        source,
        write_mode=write_mode,
        full_retry_cnt=1,
        regular_ddl_properties={"format_version": 3},
        staging_ddl_properties={"format_version": 3},
    )
    scenario.existing_types = {"stamp": source.native_type}
    paths = track_spools(monkeypatch)

    with pytest.raises(ValueError, match=r"Iceberg.*signed64 nanosecond.*range") as failure:
        scenario.run()

    assert "+10000" not in str(failure.value)
    assert scenario.loaded == []
    assert scenario.schemas == []
    assert "create_target" not in scenario.events
    assert "write_batch" not in scenario.events
    assert f"complete:{QUERY}" in scenario.events
    assert scenario.finalizations == 0
    assert "spool" not in scenario.options.input_resolution
    assert paths
    assert all(not path.exists() for path in paths)
    assert source.calls == ["POST", "GET", "GET"]
    assert len(source.statements) == 1
    assert any("system.metadata.catalogs" in statement for statement, _params in metadata_queries)
