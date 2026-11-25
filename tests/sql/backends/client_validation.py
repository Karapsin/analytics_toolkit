from __future__ import annotations

import builtins

import pytest
from analytics_toolkit.sql.backends import client_validation

from tests.sql.backends.ch._support.client_validation import client


@pytest.mark.parametrize("backend", ["gp", "trino"])
def test_other_backends_do_not_import_clickhouse_or_consume_frozen_rows(
    monkeypatch, backend
) -> None:
    original = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        assert not name.startswith(("clickhouse_driver", "clickhouse_connect"))
        return original(name, *args, **kwargs)

    def rows():
        pytest.fail("non-ClickHouse validation must not consume input")
        yield ()

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    assert not client_validation.requires_client_validation(backend, {"payload": "Object('json')"})
    assert (
        client_validation.validate_client_write_capabilities(
            backend, object(), {"payload": "Object('json')"}, rows=rows()
        )
        is None
    )


def test_generic_client_validation_keeps_declared_domain_basis_without_count() -> None:
    schema = {"amount": "SimpleAggregateFunction(sum, UInt64)"}
    assert client_validation.requires_client_validation("ch", schema)
    certificate = client_validation.validate_client_write_capabilities(
        "ch",
        client("native", context=False),
        schema,
        source_backend="ch",
        source_schema={"amount": "UInt32"},
    )
    assert certificate is not None
    assert certificate.certified_input_count is None
    assert certificate.validation_basis == "declared-source-domain"
    certificate.require_complete(123)


def test_generic_client_validation_checks_effective_http_schema_and_full_count() -> None:
    certificate = client_validation.validate_client_write_capabilities(
        "ch",
        client("http"),
        {"address": "ipv4"},
        physical_schema={"address": "IPv4"},
    )
    assert certificate is not None
    assert certificate.column_types == (("address", "IPv4"),)
    assert certificate.certified_input_count is None
    native = client_validation.validate_client_write_capabilities(
        "ch", client("native"), {"payload": "Object('json')"}, rows=[({"a": 1},)]
    )
    assert native is not None
    native.require_complete(1)
    with pytest.raises(ValueError, match="complete frozen input"):
        native.require_complete(2)


def test_generic_client_validation_preserves_publication_batch_certificate() -> None:
    certificate = client_validation.validate_client_write_capabilities(
        "ch",
        client("native"),
        {"value": "SimpleAggregateFunction(sum, UInt64)"},
        batches=[[(1,)], [(2,)]],
        source_backend="ch",
        source_schema={"value": "UInt32"},
        writer_kind="rows",
    )
    assert certificate is not None
    certificate.require_complete(2)
    assert certificate.certified_input_count == 2
    assert certificate.validation_basis == "encoded-input"
    assert certificate.geometry_basis == "publication-batches"
    assert certificate.geometry_digest is not None
