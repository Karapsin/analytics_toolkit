"""Client query certificates distinguish frozen rows from native domain proofs."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.insert import normalize_typed_row
from analytics_toolkit.sql.core.replay_spool import FrozenInputIdentity, ReplaySpool
from analytics_toolkit.sql.core.type_resolution import ResolvedColumn
from analytics_toolkit.sql.dml.query_client_validation import (
    describe_query_client_validation,
    query_validation_connection,
    validate_query_client_input,
)
from analytics_toolkit.sql.dml.query_input import PreparedQueryInput
from analytics_toolkit.sql.dml.transfer.runtime.models import TransferOptions, TransferStageState
from analytics_toolkit.sql.execution.plans import SqlOperationMetadata
from sqlglot import exp, parse_one

from tests.sql.backends.ch._support.exact_transport import native_context
from tests.sql.backends.ch._support.network_transport import encode_normalized


def prepared_input(*, native_type="Point", source_backend="ch", frozen=False):
    schema = {"value": native_type}
    options = TransferOptions(
        from_db_key=source_backend,
        from_db_backend=source_backend,
        to_db_key="ch",
        to_db_backend="ch",
        source_sql="SELECT value FROM analytics.source",
        target_table="analytics.destination",
    )
    state = TransferStageState(
        target_exists=False,
        source_columns=["value"],
        source_column_types=schema,
        stage_column_types=schema,
    )
    return PreparedQueryInput(options, state, schema, ["value"], frozen)


@pytest.mark.parametrize("checkpoint", ["missing", "uncertified"])
def test_frozen_client_query_requires_complete_input_before_certifying_or_replaying(checkpoint):
    prepared = prepared_input(native_type="UInt64", frozen=True)
    if checkpoint == "uncertified":
        prepared.options.input_resolution["spool"] = SimpleNamespace(
            certified=False, identity=FrozenInputIdentity()
        )

    with pytest.raises(ValueError, match="complete certified input checkpoint"):
        validate_query_client_input(
            prepared, {"connection": SimpleNamespace()}, python_consumer=True
        )

    assert "query_client_write_validation" not in prepared.options.input_resolution


def test_client_query_alias_without_source_domain_proof_cannot_bypass_full_input_validation():
    prepared = prepared_input(source_backend="gp")
    connection = SimpleNamespace(is_native_transport=True, context=native_context())

    with pytest.raises(ValueError, match="complete frozen input or a declared-domain proof"):
        validate_query_client_input(prepared, {"connection": connection}, python_consumer=True)

    assert "query_client_write_validation" not in prepared.options.input_resolution
    assert "spool" not in prepared.options.input_resolution


def test_client_query_native_domain_proof_retains_lazy_input_and_unknown_count():
    prepared = prepared_input()
    connection = SimpleNamespace(is_native_transport=True, context=native_context())

    validate_query_client_input(prepared, {"connection": connection}, python_consumer=True)
    metadata = SqlOperationMetadata()
    describe_query_client_validation(prepared, metadata)

    certificate = prepared.options.input_resolution["query_client_write_validation"]
    assert certificate.validation_basis == "declared-source-domain"
    assert certificate.certified_input_count is None
    assert certificate.column_types == (("value", "Point"),)
    assert "spool" not in prepared.options.input_resolution
    assert metadata.input_count is metadata.source_rows is None
    assert metadata.type_resolutions == [
        {
            "column": "value",
            "source_type": "Point",
            "target_type": "Point",
            "client_validation_basis": "declared-source-domain",
            "client_validation_driver": "native",
            "client_validation_certified_count": None,
        }
    ]


def test_client_query_constructs_factories_for_non_alias_native_types_before_writes():
    native_type = "AggregateFunction(uniq, UInt64)"
    prepared = prepared_input(native_type=native_type, source_backend="gp")
    probes = []

    def query(statement, **_kwargs):
        tree = parse_one(statement, read=ClickHouseDialect)
        assert isinstance(tree, exp.Select)
        assert tree.find(exp.Table) is None
        declarations = [literal.this for literal in tree.find_all(exp.Literal)]
        assert declarations == [native_type]
        probes.append(declarations)
        return SimpleNamespace(result_rows=[(native_type,)])

    connection = SimpleNamespace(is_native_transport=True, context=native_context(), query=query)

    with pytest.raises(ValueError, match="selected ClickHouse client cannot encode"):
        validate_query_client_input(prepared, {"connection": connection}, python_consumer=True)

    assert "query_client_write_validation" not in prepared.options.input_resolution
    assert probes == [[native_type]]


@pytest.mark.parametrize("outer_runner", [False, True])
def test_query_validation_reuses_selected_session_and_restores_outer_scope_after_failure(
    outer_runner,
):
    prepared = prepared_input()
    cached = prepared.options.input_resolution
    previous = object()
    if outer_runner:
        cached["target_connection_runner"] = previous
    target = {"connection": object()}
    original = RuntimeError("original validation failure")

    def validate_then_fail():
        with query_validation_connection(prepared.options, target):
            actual = cached["target_connection_runner"](
                "certify_client_input", lambda selected: selected["connection"]
            )
            assert actual is target["connection"]
            raise original

    with pytest.raises(RuntimeError) as failure:
        validate_then_fail()

    assert failure.value is original
    if outer_runner:
        assert cached["target_connection_runner"] is previous
    else:
        assert "target_connection_runner" not in cached


@pytest.mark.parametrize("batch_size", [1, 2])
@pytest.mark.parametrize("width", [2, 3])
def test_client_query_certifies_the_same_outer_batches_that_publication_replays(batch_size, width):
    native_type = f"SimpleAggregateFunction(any, FixedString({width}))"
    prepared = prepared_input(native_type=native_type, frozen=True)
    prepared.options = replace(prepared.options, batch_size=batch_size)
    spool = ReplaySpool(["value"])
    try:
        original = [("é",), (b"\xff\xfe",)]
        spool.write_rows(original)
        spool.certify()
        prepared.options.input_resolution.update(
            spool=spool,
            resolutions=[ResolvedColumn(native_type, source_type="String", target_backend="ch")],
        )

        validate_query_client_input(
            prepared, {"connection": SimpleNamespace()}, python_consumer=True
        )
        certificate = prepared.options.input_resolution["query_client_write_validation"]
        assert certificate.certified_input_count == 2
        assert certificate.geometry_basis == "publication-batches"
        assert certificate.column_types == (("value", native_type),)
        terminal = [
            normalize_typed_row(["value"], row, prepared.schema, native_transport=False)[0]
            for row in original
        ]
        padding = bytes(width - 2)
        assert terminal == [b"\xc3\xa9" + padding, b"\xff\xfe" + padding]
        assert encode_normalized(native_type, terminal, "http") == b"".join(terminal)
        assert list(spool.iter_rows()) == original
        assert spool.certified
        assert spool.row_count == 2
    finally:
        spool.close()
