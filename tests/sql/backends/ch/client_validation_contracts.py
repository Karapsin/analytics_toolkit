from __future__ import annotations

import datetime as dt
import struct
from types import SimpleNamespace
from uuid import UUID

import pandas as pd
import pytest
from analytics_toolkit.sql.backends.ch import client_validation as validation
from analytics_toolkit.sql.execution.cancellation import (
    AsyncSqlCancelled,
    SqlCancellationScope,
    activate_cancellation_scope,
)

from tests.sql.backends.ch._support.client_validation import client
from tests.sql.backends.ch._support.exact_transport import native_context


def offline_client(transport):
    connection = client(transport)

    def forbidden(*_args, **_kwargs):
        pytest.fail("client value certification cannot query or mutate a destination")

    connection.query = connection.command = forbidden
    return connection


def test_client_validation_mixed_native_forms_require_actual_publication_geometry() -> None:
    connection = offline_client("native")
    schema = {"payload": "String", "total": "SimpleAggregateFunction(sum, UInt64)"}
    rows = [("a", 7), (b"b", 8)]
    pending = validation.validate_client_write_capabilities(connection, schema, rows=rows)

    assert pending.certified_input_count == 2
    assert pending.geometry_basis == "publication-batches-required"
    with pytest.raises(ValueError, match="actual publication batches") as caught:
        pending.require_complete(2)
    assert caught.value.analytics_toolkit_sql_retry_safe is False

    complete = validation.validate_client_write_capabilities(connection, schema, batches=[rows])
    complete.require_complete(2)
    assert complete.geometry_basis == "publication-batches"
    assert rows == [("a", 7), (b"b", 8)]


@pytest.mark.parametrize("transport", ["native", "http"])
def test_client_validation_preserves_nullable_map_leaves_in_complete_rows(transport) -> None:
    value = {"empty": None, "id": UUID(int=1)}
    schema = {"value": "SimpleAggregateFunction(any, Map(String, Nullable(UUID)))"}
    certificate = validation.validate_client_write_capabilities(
        offline_client(transport), schema, rows=[(value,)]
    )
    certificate.require_complete(1)
    assert certificate.geometry_basis == "canonical-forms"
    assert value == {"empty": None, "id": UUID(int=1)}


@pytest.mark.parametrize("transport", ["native", "http"])
@pytest.mark.parametrize("native", ["Date32", "DateTime64(9, 'UTC')"])
def test_client_validation_temporal_domain_proofs_keep_unknown_counts(transport, native) -> None:
    certificate = validation.validate_client_write_capabilities(
        offline_client(transport), {"value": f"SimpleAggregateFunction(any, {native})"}
    )
    proof = certificate.prove_declared_domain({"value": native})
    proof.require_complete(123)
    assert proof.certified_input_count is None
    assert proof.validation_basis == "declared-source-domain"


def test_client_validation_malformed_domain_metadata_hides_private_declarations(caplog) -> None:
    certificate = validation.validate_client_write_capabilities(
        offline_client("native"), {"value": "SimpleAggregateFunction(sum, UInt64)"}
    )
    with pytest.raises(ValueError, match="complete frozen input") as caught:
        certificate.prove_declared_domain(
            {"value": "UInt64"},
            intermediate_schema={"value": "Point('private-declaration')"},
        )
    assert "private-declaration" not in str(caught.value)
    assert "private-declaration" not in caplog.text
    assert caught.value.analytics_toolkit_sql_retry_safe is False
    assert certificate.certified_input_count is None


@pytest.mark.parametrize("transport", ["native", "http"])
def test_client_validation_normalization_errors_do_not_disclose_values(transport, caplog) -> None:
    with pytest.raises(ValueError, match="Selected input") as caught:
        validation.validate_client_write_capabilities(
            offline_client(transport),
            {"value": "SimpleAggregateFunction(sum, UInt64)"},
            rows=[("private-invalid-number",)],
        )
    assert "private-invalid-number" not in str(caught.value)
    assert "private-invalid-number" not in caplog.text
    assert caught.value.analytics_toolkit_sql_retry_safe is False


def test_client_validation_incomplete_normalizer_schema_does_not_consume_input() -> None:
    def rows():
        pytest.fail("configuration identity must be complete before reading frozen input")
        yield ()

    with pytest.raises(ValueError, match="actual writer schema"):
        validation.validate_client_write_capabilities(
            offline_client("native"),
            {"value": "SimpleAggregateFunction(sum, UInt64)", "other": "UInt64"},
            normalization_schema={"value": "UInt64"},
            rows=rows(),
        )


@pytest.mark.parametrize("transport", ["native", "http"])
def test_client_validation_late_wrong_width_batch_cannot_return_partial_certificate(
    transport,
) -> None:
    with pytest.raises(ValueError, match="input does not match the actual writer schema"):
        validation.validate_client_write_capabilities(
            offline_client(transport),
            {"value": "SimpleAggregateFunction(sum, UInt64)"},
            batches=[[(7,)], [(8, 9)]],
        )


@pytest.mark.parametrize("transport", ["native", "http"])
def test_client_validation_dataframe_labels_must_match_certified_schema(transport) -> None:
    frame = pd.DataFrame({"different": [7]})
    with pytest.raises(ValueError, match="dataframe does not match the actual writer schema"):
        validation.validate_client_write_capabilities(
            offline_client(transport),
            {"value": "SimpleAggregateFunction(sum, UInt64)"},
            batches=[frame],
        )
    assert frame.columns.tolist() == ["different"]


@pytest.mark.parametrize("transport", ["native", "http"])
def test_client_validation_empty_dataframe_certifies_zero_without_encoding(transport) -> None:
    connection = offline_client(transport)
    connection.context = None
    certificate = validation.validate_client_write_capabilities(
        connection,
        {"value": "SimpleAggregateFunction(sum, UInt64)"},
        batches=[pd.DataFrame({"value": pd.Series([], dtype=object)})],
    )
    certificate.require_complete(0)
    assert certificate.certified_input_count == 0
    assert certificate.geometry_basis == "publication-batches"


def test_client_validation_rejects_invalid_real_native_block_geometry() -> None:
    from clickhouse_driver import Client  # noqa: PLC0415

    raw = Client("not-connected.invalid", settings={"insert_block_size": 0})
    raw.connection.context.server_info = native_context().server_info
    connection = SimpleNamespace(is_native_transport=True, _client=raw)
    with pytest.raises(ValueError, match="insert block size must be positive") as caught:
        validation.validate_client_write_capabilities(
            connection,
            {"value": "SimpleAggregateFunction(sum, UInt64)"},
            batches=[[(7,)]],
        )
    assert caught.value.analytics_toolkit_sql_retry_safe is False


@pytest.mark.parametrize("entrypoint", ["schema", "values"])
def test_client_validation_unknown_writer_kind_is_rejected_before_client_work(entrypoint) -> None:
    connection = offline_client("http")
    validate = {
        "schema": validation.effective_client_write_schema,
        "values": validation.validate_client_write_capabilities,
    }[entrypoint]
    with pytest.raises(ValueError, match="writer kind must be dataframe or rows"):
        validate(connection, {}, writer_kind="unknown")


@pytest.mark.parametrize("batches", [False, True])
def test_client_validation_cancellation_between_frozen_rows_keeps_primary_signal(batches) -> None:
    scope = SqlCancellationScope()

    def rows():
        yield (7,)
        scope.request_cancel()
        yield (8,)

    inputs = {"batches": ([row] for row in rows())} if batches else {"rows": rows()}
    with activate_cancellation_scope(scope), pytest.raises(AsyncSqlCancelled):
        validation.validate_client_write_capabilities(
            offline_client("native"), {"value": "SimpleAggregateFunction(sum, UInt64)"}, **inputs
        )


def test_client_validation_json_root_null_and_invalid_payload_are_distinct() -> None:
    assert validation.normalize_legacy_json_value(None, "Object('json')") is None
    with pytest.raises(ValueError, match="Object"):
        validation.normalize_legacy_json_value([1, 2], "Object('json')")


def test_client_validation_http_clock_form_requires_publication_geometry() -> None:
    from clickhouse_connect.datatypes.registry import get_from_name  # noqa: PLC0415

    schema = {"value": "SimpleAggregateFunction(any, Time)"}
    try:
        get_from_name(schema["value"])
    except Exception:  # noqa: BLE001 -- actual minimum SDKs may not register Time.
        with pytest.raises(ValueError, match="actual writer schema"):
            validation.validate_client_write_capabilities(offline_client("http"), schema)
        return
    rows = [(dt.time(12, 34, 56),)]
    pending = validation.validate_client_write_capabilities(
        offline_client("http"), schema, rows=rows
    )
    with pytest.raises(ValueError, match="actual publication batches"):
        pending.require_complete(1)
    complete = validation.validate_client_write_capabilities(
        offline_client("http"), schema, batches=[rows]
    )
    complete.require_complete(1)


def test_client_validation_passes_batch_to_actual_native_data_dependent_prefix(monkeypatch) -> None:
    connection = offline_client("native")
    try:
        validation._native_column("JSON", connection.context)
    except Exception:  # noqa: BLE001 -- older native SDKs do not support the modern JSON type.
        with pytest.raises(ValueError, match="actual writer schema"):
            validation.validate_client_write_capabilities(connection, {"value": "JSON"})
        return
    output = bytearray()

    def sendall(_socket, payload):
        output.extend(payload)

    monkeypatch.setattr(validation._DiscardSocket, "sendall", sendall)
    certificate = validation.validate_client_write_capabilities(
        connection,
        {"value": "JSON"},
        normalization_schema={"value": "SimpleAggregateFunction(any, JSON)"},
        batches=[[({"a": 1},), ({"b": 2},)]],
    )
    certificate.require_complete(2)
    assert output[:8] == struct.pack("<Q", 2)
