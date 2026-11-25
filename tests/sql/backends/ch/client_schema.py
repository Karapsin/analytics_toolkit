from __future__ import annotations

from types import SimpleNamespace

import pytest
from analytics_toolkit.sql.backends import client_validation as dispatch
from analytics_toolkit.sql.backends.ch import client_validation as validation
from analytics_toolkit.sql.backends.ch.client_schema import resolve_prospective_schema
from sqlglot import exp, parse_one

from tests.sql.backends.ch._support.client_validation import client


@pytest.mark.parametrize("transport", ["http", "native"])
def test_prospective_types_use_destination_canonical_name_and_original_normalizer(
    transport,
) -> None:
    connection = client(transport)
    queries = []

    def query(sql):
        queries.append(sql)
        return SimpleNamespace(result_rows=[("Int64",)])

    connection.query = query
    certificate = dispatch.validate_client_write_capabilities(
        "ch",
        connection,
        {"value": "BIGINT"},
        batches=[[(42,)]],
        prospective_schema=True,
    )
    certificate.require_complete(1)
    assert certificate.column_types == (("value", "Int64"),)
    assert certificate.normalization_types == (("value", "BIGINT"),)
    assert certificate.certified_input_count == 1
    assert queries == ["SELECT toTypeName(defaultValueOfTypeName('BIGINT'))"]


@pytest.mark.parametrize("prospective", [False, True])
def test_actual_http_row_headers_never_resolve_prospective_aliases(prospective) -> None:
    connection = client("http")

    def query(_sql):
        pytest.fail("supplied HTTP Native-format headers must retain their raw types")

    connection.query = query
    with pytest.raises(ValueError, match="actual writer schema"):
        dispatch.validate_client_write_capabilities(
            "ch",
            connection,
            {"value": "BIGINT"},
            physical_schema={"value": "Int64"},
            writer_kind="rows",
            prospective_schema=prospective,
        )


@pytest.mark.parametrize("transport", ["native", "http"])
def test_observed_or_supported_writer_types_do_not_query_for_aliases(transport) -> None:
    connection = client(transport)

    def query(_sql):
        pytest.fail("canonicalization is allowed only after a prospective factory failure")

    connection.query = query
    with pytest.raises(ValueError, match="actual writer schema"):
        validation.validate_client_write_capabilities(connection, {"value": "BIGINT"})
    certificate = validation.validate_client_write_capabilities(
        connection, {"value": "Int64"}, prospective_schema=True
    )
    assert certificate.certified_input_count is None


def test_invalid_normalization_hint_does_not_authorize_a_prospective_probe() -> None:
    connection = client("http")

    def query(_sql):
        pytest.fail("only an actual writer factory failure authorizes canonicalization")

    connection.query = query
    with pytest.raises(ValueError, match="actual writer schema"):
        validation.validate_client_write_capabilities(
            connection,
            {"value": "Int64"},
            normalization_schema={"value": "UnbalancedType("},
            prospective_schema=True,
        )


@pytest.mark.parametrize("transport", ["native", "http"])
def test_destination_canonical_type_must_have_an_actual_selected_encoder(transport) -> None:
    connection = client(transport)
    connection.query = lambda _sql: SimpleNamespace(result_rows=[("UnsupportedToolkitCodec",)])
    with pytest.raises(ValueError, match="actual writer schema") as caught:
        validation.validate_client_write_capabilities(
            connection, {"value": "BIGINT"}, prospective_schema=True
        )
    assert caught.value.analytics_toolkit_sql_retry_safe is False


@pytest.mark.parametrize("transport", ["native", "http"])
def test_prospective_alias_still_requires_complete_actual_encoder_value_proof(transport) -> None:
    connection = client(transport)
    connection.query = lambda _sql: SimpleNamespace(result_rows=[("Int64",)])
    with pytest.raises(ValueError, match="Selected input"):
        validation.validate_client_write_capabilities(
            connection,
            {"value": "BIGINT"},
            batches=[[(2**100,)]],
            prospective_schema=True,
        )


@pytest.mark.parametrize("rows", [None, [], [()], [("",)], [(1,)], [("Int64",), ("Int64",)]])
def test_prospective_type_response_must_be_one_complete_typed_row(rows) -> None:
    connection = SimpleNamespace(query=lambda _sql: SimpleNamespace(result_rows=rows))
    with pytest.raises(ValueError, match="destination metadata") as caught:
        resolve_prospective_schema(connection, {"value": "BIGINT"})
    assert caught.value.analytics_toolkit_sql_retry_safe is False


def test_prospective_type_names_are_single_clickhouse_string_literals() -> None:
    raw = "Unknown('private\\type', 'it\\'s')"
    queries = []

    def query(sql):
        queries.append(sql)
        return SimpleNamespace(result_rows=[("Int64",)])

    assert resolve_prospective_schema(SimpleNamespace(query=query), {"value": raw}) == {
        "value": "Int64"
    }
    parsed = parse_one(queries[0], read="clickhouse")
    assert not list(parsed.find_all(exp.Table))
    assert [literal.this for literal in parsed.find_all(exp.Literal)] == [raw]


def test_prospective_probe_failure_retains_its_primary_retry_classification() -> None:
    original = TimeoutError("destination probe timed out")

    def query(_sql):
        raise original

    connection = client("native")
    connection.query = query
    with pytest.raises(TimeoutError) as caught:
        validation.validate_client_write_capabilities(
            connection, {"value": "BIGINT"}, prospective_schema=True
        )
    assert caught.value is original
