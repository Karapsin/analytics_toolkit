"""Public transfers preserve server JSON documents before driver decoding."""

from __future__ import annotations

import pytest
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.gp.json_values import JsonDocument
from analytics_toolkit.sql.backends.native_scalars import ExactInterval
from analytics_toolkit.sql.core.exact_values import ExactClock
from analytics_toolkit.sql.core.native_scalars import ExactArray
from psycopg2.extensions import adapt

from tests.sql._support.gp_source_codecs import install_source_transfer
from tests.sql._support.load_encoded_values import EncodedConnection


@pytest.mark.parametrize("frozen", [False, True], ids=["declared-fit", "frozen"])
@pytest.mark.parametrize(
    "document",
    ['"1"', '"hello"', "12345678901234567890.123456789", "null", None],
    ids=["numeric-string", "ordinary-string", "decimal-token", "json-null", "sql-null"],
)
def test_gp_source_json_document_survives_public_transfer(monkeypatch, document, frozen):
    scenario, connection = install_source_transfer(
        monkeypatch, "JSONB", 3802, [document], frozen=frozen
    )

    result = scenario.run(metadata=True)

    assert result.rows == 1
    assert scenario.source_open_count == scenario.read_count == 1
    assert len(connection.queries) == 1
    assert connection.closed
    assert all(cursor.closed for cursor in connection.cursors)
    value = scenario.loaded[0][0][0]
    if document is None:
        assert value is None
    else:
        assert isinstance(value, JsonDocument)
        assert value == document
        # Real psycopg2 adaptation must preserve the encoded JSON scalar,
        # rather than binding an unquoted decoded string or binary float.
        assert adapt(value).getquoted() == adapt(document).getquoted()
    assert scenario.schemas == [{"id": "JSONB"}]
    assert scenario.options.table_schema == {"id": "JSONB"}


def test_gp_source_scalar_json_oid_retains_native_metadata():
    assert get_backend_adapter("gp").type_code_name(114, None, None) == "json"


def test_gp_public_json_transfer_uses_real_dbapi_description_identity(monkeypatch):
    scenario, connection = install_source_transfer(
        monkeypatch, "JSON", 114, ['"1"', "null", None], frozen=False
    )
    scenario.source_schema = get_backend_adapter("gp").inspect_source_query_schema(
        connection, scenario.options.source_sql
    )

    result = scenario.run(metadata=True)

    assert scenario.source_schema[0].native_type == "json"
    assert result.rows == 3
    assert scenario.loaded == [[(JsonDocument('"1"'),), (JsonDocument("null"),), (None,)]]
    assert isinstance(scenario.loaded[0][0][0], JsonDocument)
    assert isinstance(scenario.loaded[0][1][0], JsonDocument)
    assert scenario.source_open_count == scenario.read_count == 1
    assert len(connection.queries) == 2
    assert "WHERE 1 = 0" in connection.queries[0]
    assert "WHERE 1 = 0" not in connection.queries[1]
    assert all(cursor.closed for cursor in connection.cursors)


@pytest.mark.parametrize("frozen", [False, True], ids=["declared-fit", "narrow-authority"])
def test_gp_public_array_transfer_keeps_actual_dimensions_and_bounds(monkeypatch, frozen):
    scenario, source = install_source_transfer(
        monkeypatch,
        "INTEGER[]",
        1016 if frozen else 1007,
        ["[-2:-1][0:1]={{10,NULL},{20,30}}"],
        frozen=frozen,
    )

    result = scenario.run(metadata=True)

    assert result.rows == 1
    assert scenario.source_open_count == scenario.read_count == 1
    assert source.closed
    assert all(cursor.closed for cursor in source.cursors)
    decoded = scenario.loaded[0][0][0]
    assert isinstance(decoded, ExactArray)
    assert decoded.values == (10, None, 20, 30)
    assert decoded.dimensions == (2, 2)
    assert decoded.lower_bounds == (-2, 0)
    assert result.metadata.input_count == (1 if frozen else None)
    destination = EncodedConnection()
    get_backend_adapter("gp").insert_rows_batch(
        destination,
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
    expected = '[-2:-1][0:1]={{"10",NULL},{"20","30"}}'
    assert destination.bound_rows == [(expected,)]
    assert adapt(expected).getquoted().decode() in destination.executed[0]


@pytest.mark.parametrize("frozen", [False, True], ids=["declared-fit", "json-to-jsonb"])
def test_gp_public_json_array_transfer_keeps_sql_rank_and_both_null_identities(monkeypatch, frozen):
    document = '{{"{\\"x\\":1}",NULL},{"null","\\"null\\""}}'
    scenario, _source = install_source_transfer(
        monkeypatch, "JSONB[]", 199 if frozen else 3807, [document], frozen=frozen
    )

    result = scenario.run(metadata=True)
    assert result.rows == 1
    assert result.metadata.input_count == (1 if frozen else None)

    decoded = scenario.loaded[0][0][0]
    assert isinstance(decoded, ExactArray)
    assert decoded.dimensions == (2, 2)
    assert decoded.values == (
        JsonDocument('{"x":1}'),
        None,
        JsonDocument("null"),
        JsonDocument('"null"'),
    )
    destination = EncodedConnection()
    get_backend_adapter("gp").insert_rows_batch(
        destination,
        "sandbox.target",
        ["id"],
        [(decoded,)],
        target_column_types={"id": "JSONB[]"},
        trino_insert_chunk_size=None,
        gp_insert_chunk_size=10,
        connection_type="gp",
        query_label=None,
        on_progress=None,
    )
    assert destination.bound_rows == [("[1:2][1:2]=" + document,)]


@pytest.mark.parametrize(
    "case",
    [
        ("TIME", 1083, "24:00:00", ExactClock(86400 * 10**6, 6), "24:00:00.000000"),
        ("INTERVAL", 1186, "1 mon", ExactInterval(1, 0, 0), "+1 mons +0 days +0 microseconds"),
        (
            "TIME[]",
            1183,
            '[0:1]={"24:00:00",NULL}',
            ExactArray((ExactClock(86400 * 10**6, 6), None), (2,), (0,)),
            '[0:1]={"24:00:00.000000",NULL}',
        ),
        (
            "INTERVAL[]",
            1187,
            '[0:1]={"1 mon","30 days"}',
            ExactArray((ExactInterval(1, 0, 0), ExactInterval(0, 30, 0)), (2,), (0,)),
            '[0:1]={"+1 mons +0 days +0 microseconds","+0 mons +30 days +0 microseconds"}',
        ),
    ],
    ids=["end-of-day", "calendar-month", "bounded-end-of-day-array", "bounded-month-day-array"],
)
def test_gp_temporal_source_transfer_keeps_native_components_before_driver_loss(monkeypatch, case):
    native, oid, document, expected, wire = case
    scenario, source = install_source_transfer(monkeypatch, native, oid, [document], frozen=False)

    assert scenario.run() == 1

    value = scenario.loaded[0][0][0]
    assert value == expected
    assert scenario.source_open_count == scenario.read_count == len(source.queries) == 1
    assert source.closed
    assert all(cursor.closed for cursor in source.cursors)
    destination = EncodedConnection()
    get_backend_adapter("gp").insert_rows_batch(
        destination,
        "sandbox.target",
        ["id"],
        [(value,)],
        target_column_types={"id": native},
        trino_insert_chunk_size=None,
        gp_insert_chunk_size=10,
        connection_type="gp",
        query_label=None,
        on_progress=None,
    )
    assert destination.bound_rows == [(wire,)]
