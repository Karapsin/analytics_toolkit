"""Native VARBINARY decoding must not depend on the installed SDK version."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from analytics_toolkit.sql.backends.trino import exact_reads
from analytics_toolkit.sql.backends.type_resolution import ResolvedColumn


class _NoOpValueMapper:
    def map(self, value: Any) -> Any:
        return value


@pytest.fixture
def minimum_binary_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    interface = exact_reads._sdk_interface()
    base = interface.mapper_factory

    class MinimumBinaryFactory(base):
        def _create_value_mapper(self, signature: dict[str, Any]) -> Any:
            # Official SDK 0.320.0 delegates unregistered VARBINARY to NoOp.
            if signature["rawType"] == "varbinary":
                return _NoOpValueMapper()
            return super()._create_value_mapper(signature)

    minimum = replace(interface, mapper_factory=MinimumBinaryFactory)
    monkeypatch.setattr(exact_reads, "_sdk_interface", lambda: minimum)


@pytest.mark.parametrize(("wire", "expected"), [("AP8=", b"\x00\xff"), ("", b""), (None, None)])
@pytest.mark.parametrize(("backend", "native"), [("gp", "bytea"), ("trino", "varbinary")])
def test_exact_binary_reads_minimum_sdk_preserves_source_bytes_through_destination_conversion(
    minimum_binary_factory: None,
    wire: str | None,
    expected: bytes | None,
    backend: str,
    native: str,
) -> None:
    del minimum_binary_factory
    columns = [{"typeSignature": {"rawType": "varbinary", "arguments": []}}]

    value = exact_reads._RowMapper(columns).map([[wire]])[0][0]
    converted = ResolvedColumn(
        native, source_type="varbinary", source_backend="trino", target_backend=backend
    ).convert(value)

    assert value == expected
    assert converted == expected


@pytest.mark.parametrize("wire", ["private-invalid-base64", "private-\N{EURO SIGN}", 42])
def test_exact_binary_reads_invalid_native_response_fails_without_private_values(wire: Any) -> None:
    columns = [{"typeSignature": {"rawType": "varbinary", "arguments": []}}]

    with pytest.raises(ValueError, match="Trino VARBINARY response") as error:
        exact_reads._RowMapper(columns).map([[wire]])

    assert "private" not in str(error.value)
