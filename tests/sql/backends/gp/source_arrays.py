"""Raw source array callbacks retain calendar and array-bound identities."""

from __future__ import annotations

import pytest
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.gp.source_arrays import SourceArrayError
from analytics_toolkit.sql.backends.gp.source_codecs import prepare_exact_source_cursor
from analytics_toolkit.sql.backends.gp.source_temporals import SourceTemporalError
from analytics_toolkit.sql.core.native_scalars import (
    ExactArray,
    ExactInterval,
    JsonDocument,
    NativeDecimalNaN,
    NativeFloatNaN,
)
from psycopg2 import extensions
from psycopg2.extensions import adapt

from tests.sql._support.load_encoded_values import EncodedConnection


def exact_callbacks(monkeypatch):
    callbacks = {}
    registered = {}
    original = extensions.new_type
    scope = object()

    def new_type(oids, name, callback):
        caster = original(oids, name, callback)
        callbacks[id(caster)] = callback
        return caster

    def register(caster, cursor=None):
        assert cursor is scope
        for oid in caster.values:
            registered[oid] = callbacks[id(caster)]

    monkeypatch.setattr(extensions, "new_type", new_type)
    monkeypatch.setattr(extensions, "register_type", register)
    prepare_exact_source_cursor(object(), scope)
    return registered


@pytest.mark.parametrize(
    "case",
    [
        (1007, "[0:1]={10,20}", (10, 20), (2,), (0,)),
        (1007, "[-2:-1][0:1]={{10,NULL},{20,30}}", (10, None, 20, 30), (2, 2), (-2, 0)),
        (1007, "{}", (), (), ()),
        (
            3807,
            '{{"null",NULL},{"\\"1\\"","12345678901234567890.123456789"}}',
            (
                JsonDocument("null"),
                None,
                JsonDocument('"1"'),
                JsonDocument("12345678901234567890.123456789"),
            ),
            (2, 2),
            (1, 1),
        ),
        (
            1187,
            '{"1 mon","30 days"}',
            (ExactInterval(1, 0, 0), ExactInterval(0, 30, 0)),
            (2,),
            (1,),
        ),
    ],
    ids=["zero-bound", "mixed-bounds", "empty", "json-topology", "calendar-month"],
)
def test_gp_raw_array_typecaster_callback_preserves_cells_and_bounds(monkeypatch, case):
    oid, document, values, dimensions, bounds = case
    callbacks = exact_callbacks(monkeypatch)
    assert oid in callbacks, "Internal source arrays require an exact decoder before fetch"
    # Invoke the registered Python decoder, not its unsafe cursor-dependent C bridge.
    decoded = callbacks[oid](document, None)
    assert isinstance(decoded, ExactArray)
    assert decoded.values == values
    assert decoded.dimensions == dimensions
    assert decoded.lower_bounds == bounds
    assert callbacks[oid](None, None) is None


def test_gp_exact_time_callback_keeps_native_end_of_day(monkeypatch):
    callbacks = exact_callbacks(monkeypatch)
    assert 1083 in callbacks
    decoded = callbacks[1083]("24:00:00", None)
    assert decoded.ticks == 86400 * 10**6
    assert decoded.scale == 6
    assert decoded.isoformat() == "24:00:00.000000"


def test_gp_ordinary_list_adaptation_cannot_preserve_source_lower_bound():
    # This actual psycopg2 output is why retaining only the decoded Python
    # list cannot certify a native source with lower bound zero.
    assert adapt([10, 20]).getquoted() == b"ARRAY[10,20]"


def test_gp_bounded_source_array_reaches_real_psycopg_binding_without_rebasing(monkeypatch):
    decoded = exact_callbacks(monkeypatch)[1007]("[0:1]={10,20}", None)
    connection = EncodedConnection()
    get_backend_adapter("gp").insert_rows_batch(
        connection,
        "sandbox.target",
        ["id"],
        [(decoded,)],
        target_column_types={"id": "INTEGER[]"},
        trino_insert_chunk_size=None,
        gp_insert_chunk_size=10,
        connection_type="gp",
        query_label=None,
        on_progress=None,
    )
    expected = '[0:1]={"10","20"}'
    assert connection.bound_rows == [(expected,)]
    assert adapt(expected).getquoted().decode() in connection.executed[0]


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        ("1 mon", ExactInterval(1, 0, 0)),
        ("1 year 2 mons -3 days 04:05:06.123456", ExactInterval(14, -3, 14706123456)),
        (
            "@ 1 year 2 mons 3 days 4 hours 5 mins 6.123456 secs ago",
            ExactInterval(-14, -3, -14706123456),
        ),
        ("P1Y2M-3DT4H5M6.123456S", ExactInterval(14, -3, 14706123456)),
        ("+1-2 -3 +4:05:06.123456", ExactInterval(14, -3, 14706123456)),
    ],
)
def test_gp_interval_source_callback_preserves_all_native_output_styles(
    monkeypatch, document, expected
):
    assert exact_callbacks(monkeypatch)[1186](document, None) == expected


def test_gp_native_end_of_day_reaches_real_psycopg_binding(monkeypatch):
    decoded = exact_callbacks(monkeypatch)[1083]("24:00:00", None)
    connection = EncodedConnection()
    get_backend_adapter("gp").insert_rows_batch(
        connection,
        "sandbox.target",
        ["id"],
        [(decoded,)],
        target_column_types={"id": "TIME"},
        trino_insert_chunk_size=None,
        gp_insert_chunk_size=10,
        connection_type="gp",
        query_label=None,
        on_progress=None,
    )
    assert connection.bound_rows == [("24:00:00.000000",)]


@pytest.mark.parametrize(
    ("oid", "native", "carrier"),
    [
        (700, "REAL", NativeFloatNaN),
        (701, "DOUBLE PRECISION", NativeFloatNaN),
        (1700, "NUMERIC", NativeDecimalNaN),
    ],
)
def test_gp_native_source_nan_is_not_a_sql_null_at_driver_binding(
    monkeypatch, oid, native, carrier
):
    callbacks = exact_callbacks(monkeypatch)
    assert oid in callbacks
    decoded = callbacks[oid]("NaN", None)
    assert isinstance(decoded, carrier)
    assert callbacks[oid](None, None) is None
    connection = EncodedConnection()
    get_backend_adapter("gp").insert_rows_batch(
        connection,
        "sandbox.target",
        ["id"],
        [(decoded,), (None,)],
        target_column_types={"id": native},
        trino_insert_chunk_size=None,
        gp_insert_chunk_size=10,
        connection_type="gp",
        query_label=None,
        on_progress=None,
    )
    assert connection.bound_rows[0][0] is not None
    assert connection.bound_rows[1][0] is None
    assert "NaN" in connection.executed[0]


@pytest.mark.parametrize(
    "document",
    ["[0:2]={1,2}", "{{1},{2,3}}", '{"private-unclosed}', "{1,}", "{} trailing"],
)
def test_gp_incomplete_array_input_cannot_become_a_native_checkpoint(monkeypatch, document):
    with pytest.raises(SourceArrayError, match="exact native source array") as error:
        exact_callbacks(monkeypatch)[1007](document, None)
    assert "private-unclosed" not in str(error.value)
    assert error.value.analytics_toolkit_sql_retry_safe is False


@pytest.mark.parametrize("document", ["25:00:00", "24:00:00.000001", "private-clock"])
def test_gp_invalid_source_clock_is_rejected_without_payload_echo(monkeypatch, document):
    with pytest.raises(SourceTemporalError, match="exact native source temporal") as error:
        exact_callbacks(monkeypatch)[1083](document, None)
    assert "private-clock" not in str(error.value)


def test_gp_source_array_does_not_call_a_c_scalar_decoder_without_its_real_cursor(monkeypatch):
    with pytest.raises(SourceArrayError, match="exact native source array"):
        exact_callbacks(monkeypatch)[1182]("{2025-01-02}", None)
