"""Deferred native assignments retain values without claiming local certification."""

from __future__ import annotations

from decimal import Decimal
from enum import Enum
from uuid import UUID

import pandas as pd
import pytest
from analytics_toolkit.sql.backends.conversion_profiles import validate_converted_rows
from analytics_toolkit.sql.backends.models import SourceColumn
from analytics_toolkit.sql.backends.native_values import requires_native_assignment
from analytics_toolkit.sql.backends.type_resolution import (
    ColumnProfile,
    ResolvedColumn,
    resolve_column_type,
    resolve_profile,
)
from analytics_toolkit.sql.core import type_resolution as compatibility


def _profile(*values):
    result = ColumnProfile()
    for value in values:
        result.add(value)
    return result


def _pending(value, native_type="CIDR"):
    return resolve_profile(
        _profile(value),
        "gp",
        authoritative_type=native_type,
        allow_pending_native_assignment=True,
    )


class NativeValue(Enum):
    ADDRESS = "192.0.2.0/24"


@pytest.mark.parametrize(
    "native_type",
    ["CIDR", "MACADDR", '"app"."String"', '"event.types"."amount"', "app.address[]"],
)
def test_pending_native_profiles_identify_server_owned_types_without_renaming(native_type):
    assert requires_native_assignment(native_type, "gp")
    resolution = _pending("192.0.2.0/24", native_type)
    assert resolution.target_type == native_type
    assert resolution.native_assignment_required
    assert not resolution.text_fallback


@pytest.mark.parametrize(
    "native_type",
    ["INTEGER", "pg_catalog.int8", "INET", "JSONB", "TIMETZ", "NUMERIC(12, 4)", "UUID[]"],
)
def test_pending_native_profiles_keep_supported_gp_families_local(native_type):
    assert not requires_native_assignment(native_type, "gp")


@pytest.mark.parametrize("backend", ["ch", "trino"])
def test_pending_native_profiles_do_not_enable_other_backend_unknowns(backend):
    assert not requires_native_assignment("CIDR", backend)
    message = (
        "Invalid or unsupported Trino native type declaration"
        if backend == "trino"
        else "Cannot certify"
    )
    with pytest.raises(ValueError, match=message):
        resolve_profile(
            _profile("192.0.2.0/24"),
            backend,
            authoritative_type="CIDR",
            allow_pending_native_assignment=True,
        )


@pytest.mark.parametrize("native_type", ["CIDR", "MACADDR", '"app"."amount"'])
def test_pending_native_profiles_default_validation_does_not_publish_unknowns(native_type):
    with pytest.raises(ValueError, match="Cannot certify"):
        resolve_profile(_profile("192.0.2.0/24"), "gp", authoritative_type=native_type)


@pytest.mark.parametrize(
    ("native_type", "value"),
    [
        ("String", Decimal("12345678901234567890.123456789")),
        ("Float64", 2**53 + 1),
        ('"app"."uuid_value"', UUID("d3e33303-453e-40b0-bcd6-dc1c3d93c907")),
        ('"app"."binary_value"', b"\x00\xff"),
        ('"app"."nested_value"', [Decimal("0.0001"), None, (2**128 + 1, b"\x00")]),
        ('"app"."mapping_value"', {"amount": Decimal("1.2000"), "raw": b"\xff"}),
    ],
)
def test_pending_native_profiles_preserve_exact_objects_until_native_assignment(native_type, value):
    resolution = _pending(value, native_type)
    assert resolution.convert(value) is value
    assert resolution.convert(resolution.convert(value)) is value
    profiles = validate_converted_rows([(value,)], [resolution], backend="gp")
    assert profiles[0].count == 1
    assert resolution.certified_input_count == 0
    assert resolution.metadata()["reason"] == "native_assignment_pending"
    assert resolution.metadata()["native_assignment_required"] is True
    assert resolution.metadata()["encoding_version"] is None


@pytest.mark.parametrize("value", [None, pd.NA, float("nan")])
def test_pending_native_profiles_keep_nulls_without_optimistic_certification(value):
    resolution = _pending(value)
    assert resolution.convert(value) is None
    profiles = validate_converted_rows([(value,)], [resolution], backend="gp")
    assert profiles[0].count == profiles[0].null_count == 1
    assert resolution.metadata()["certified_input_count"] == 0


def test_pending_native_profiles_unwrap_enums_without_stringifying_values():
    resolution = _pending(NativeValue.ADDRESS)
    assert resolution.convert(NativeValue.ADDRESS) is NativeValue.ADDRESS.value


def test_pending_native_profiles_observe_each_position_once_without_certifying_native_input():
    resolutions = [_pending(2**128 + 1, '"app"."large"'), _pending(Decimal("0.0001"))]
    original = [(2**128 + 1, None), (None, Decimal("0.0001"))]
    visited = []

    def rows():
        for row in original:
            visited.append(row)
            yield row

    profiles = validate_converted_rows(rows(), resolutions, backend="gp")
    assert visited == original
    assert len(profiles) == 2
    assert profiles[0].minimum_integer == profiles[0].maximum_integer == 2**128 + 1
    assert profiles[0].count == profiles[1].count == 2
    assert profiles[0].null_count == profiles[1].null_count == 1
    assert profiles[1].families == {"decimal"}
    assert all(resolution.certified_input_count == 0 for resolution in resolutions)


@pytest.mark.parametrize("backend", ["gp", "trino", "ch"])
def test_pending_native_profiles_reject_pending_flags_on_locally_supported_integers(backend):
    resolution = ResolvedColumn("SMALLINT", target_backend=backend, native_assignment_required=True)
    with pytest.raises(ValueError, match="native assignment"):
        validate_converted_rows([(2**80,)], [resolution], backend=backend)


@pytest.mark.parametrize("backend", ["gp", "trino", "ch"])
def test_pending_native_profiles_opt_in_keeps_known_integer_constraints_strict(backend):
    with pytest.raises(ValueError, match="authoritative integer"):
        resolve_profile(
            _profile(2**80),
            backend,
            authoritative_type="SMALLINT",
            allow_pending_native_assignment=True,
        )


@pytest.mark.parametrize("backend", ["trino", "ch"])
def test_pending_native_profiles_forced_flags_cannot_certify_unknown_other_backend_types(backend):
    resolution = ResolvedColumn(
        "unregistered_type", target_backend=backend, native_assignment_required=True
    )
    with pytest.raises(ValueError, match="native assignment"):
        validate_converted_rows([("uncertified input",)], [resolution], backend=backend)


@pytest.mark.parametrize(
    ("backend", "resolution_backend", "native_type"),
    [
        ("gp", "trino", "CIDR"),
        ("trino", "gp", "CIDR"),
        ("gp", "gp", "INTEGER"),
        ("trino", "trino", "unregistered_type"),
        ("ch", "ch", "unregistered_type"),
    ],
)
def test_pending_native_profiles_empty_input_still_rejects_invalid_pending_state(
    backend, resolution_backend, native_type
):
    resolution = ResolvedColumn(
        native_type, target_backend=resolution_backend, native_assignment_required=True
    )
    with pytest.raises(ValueError, match="native assignment"):
        validate_converted_rows([], [resolution], backend=backend)


def test_pending_native_profiles_invalid_pending_state_is_rejected_before_input_consumption():
    visited = []

    def rows():
        visited.append(True)
        yield ("not scanned",)

    resolution = ResolvedColumn("CIDR", target_backend="trino", native_assignment_required=True)
    with pytest.raises(ValueError, match="native assignment"):
        validate_converted_rows(rows(), [resolution], backend="gp")
    assert visited == []


def test_pending_native_profiles_supported_authority_keeps_existing_metadata_shape():
    resolution = resolve_profile(
        _profile(42),
        "gp",
        authoritative_type="INTEGER",
        allow_pending_native_assignment=True,
    )
    assert not resolution.native_assignment_required
    assert resolution.convert(42) == 42
    assert resolution.certified_input_count == 1
    metadata = resolution.metadata()
    assert metadata["destination_type"] == "INTEGER"
    assert metadata["reason"] == "authoritative"
    assert "native_assignment_required" not in metadata


def test_pending_native_profiles_compatibility_imports_retain_source_metadata():
    assert compatibility.ResolvedColumn is ResolvedColumn
    assert compatibility.resolve_column_type is resolve_column_type
    source = SourceColumn("address", "TEXT")
    resolution = compatibility.resolve_column_type(
        source,
        "gp",
        _profile("192.0.2.0/24"),
        source_backend="trino",
        authoritative_type="CIDR",
        allow_pending_native_assignment=True,
    )
    assert resolution.source_type == "TEXT"
    assert resolution.source_backend == "trino"
    assert resolution.target_type == "CIDR"
    assert resolution.certified_input_count == 0
    assert resolution.convert(NativeValue.ADDRESS.value) is NativeValue.ADDRESS.value
