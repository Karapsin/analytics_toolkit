"""Metadata proofs must cover full native ranges before skipping input validation."""

import datetime as dt

import pytest
from analytics_toolkit.sql.backends.type_resolution import (
    ColumnProfile,
    declared_domain_fits,
    resolve_profile,
    validate_profile,
)


@pytest.mark.parametrize(
    ("source", "target", "source_backend", "target_backend", "fits"),
    [
        ("Date32", "Date", "ch", "ch", False),
        ("Date", "Date32", "ch", "ch", True),
        ("DATE", "Date", "gp", "ch", False),
        ("DATE", "DATE", "gp", "trino", False),
        ("Date32", "DATE", "ch", "gp", True),
        ("Date32", "BIGINT", "ch", "gp", False),
        ("Float64", "Float32", "ch", "ch", False),
        ("Float32", "Float64", "ch", "ch", True),
        ("DOUBLE PRECISION", "REAL", "gp", "gp", False),
        ("float8", "float4", "gp", "gp", False),
        ("float4", "DOUBLE", "gp", "trino", True),
        ("FLOAT", "REAL", "gp", "gp", False),
        ("FLOAT(20)", "DOUBLE PRECISION", "gp", "gp", True),
        ("FLOAT(40)", "REAL", "gp", "gp", False),
        ("FLOAT(40)", "Float32", "ch", "ch", True),
        ("SINGLE", "Float64", "ch", "ch", True),
        ("Float64", "VARCHAR", "ch", "trino", False),
        ("DateTime64(0)", "DateTime", "ch", "ch", False),
        ("DateTime", "DateTime64(0)", "ch", "ch", True),
        ("DateTime64(6)", "DateTime64(9)", "ch", "ch", False),
        ("DateTime64(3)", "DateTime64(6)", "ch", "ch", True),
        ("DateTime64(6)", "TIMESTAMP(6)", "ch", "gp", True),
        ("TIMESTAMP(6)", "DateTime64(6)", "gp", "ch", False),
        ("TIMESTAMP(6)", "Nullable(DateTime64(6))", "gp", "ch", False),
        ("TIMESTAMP(6)", "TIMESTAMP(9)", "gp", "trino", False),
        ("VARBINARY(10)", "VARBINARY(2)", "trino", "trino", False),
        ("BINARY(2)", "VARBINARY(10)", "gp", "trino", True),
        ("BYTEA", "VARBINARY", "gp", "trino", True),
        ("BYTEA", "VARCHAR", "gp", "trino", False),
    ],
)
def test_proof_requires_destination_to_cover_entire_source_domain(
    source, target, source_backend, target_backend, fits
):
    assert (
        declared_domain_fits(source, target, target_backend, source_backend=source_backend) is fits
    )


@pytest.mark.parametrize(
    ("value", "native", "error"),
    [
        (dt.date(1900, 1, 1), "Date", "date range"),
        (1e100, "Float32", "floating-point domain"),
        (1e100, "FLOAT(40)", "floating-point domain"),
        (dt.datetime(2263, 1, 1, tzinfo=dt.timezone.utc), "DateTime64(9)", "timestamp range"),
    ],
)
def test_profile_rejects_actual_authoritative_narrowing_before_publication(value, native, error):
    profile = ColumnProfile()
    profile.add(value)
    with pytest.raises(ValueError, match=error):
        validate_profile(profile, native, "ch", allow_timestamp_precision_loss=False)


def test_bounded_binary_checks_actual_last_value_and_unqualified_ch_timestamp_identity():
    assert declared_domain_fits("DateTime64(9)", "DateTime64(9)", "ch")
    profile = ColumnProfile()
    profile.add(b"a")
    profile.add(b"long")
    with pytest.raises(ValueError, match="binary length"):
        validate_profile(profile, "VARBINARY(2)", "trino")


@pytest.mark.parametrize(("source", "backend"), [("String", "ch"), ("VARCHAR", "trino")])
def test_text_metadata_cannot_prove_greenplum_text_compatibility(source, backend):
    assert not declared_domain_fits(source, "TEXT", "gp", source_backend=backend)
    assert not declared_domain_fits("String", "VARCHAR", "trino", source_backend="ch")


def test_binary_clickhouse_string_data_remains_native_bytea():
    profile = ColumnProfile()
    profile.add(b"\xff\x00")
    resolved = resolve_profile(profile, "gp", source_type="String", source_backend="ch")
    assert resolved.target_type == "BYTEA"
    assert resolved.convert(b"\xff\x00") == b"\xff\x00"
    for value in [b"\xff", "with\x00nul", "\ud800"]:
        profile = ColumnProfile()
        profile.add(value)
        with pytest.raises(ValueError, match="authoritative Greenplum text"):
            validate_profile(profile, "TEXT", "gp")
    profile = ColumnProfile()
    other = ColumnProfile()
    other.add("with\x00nul")
    profile.merge(other)
    assert profile.text_has_nul


def test_inferred_nul_strings_choose_native_bytea_with_exact_utf8_conversion():
    profile = ColumnProfile()
    profile.add("é\x00text")
    profile.add("normal")
    resolved = resolve_profile(profile, "gp", source_type="String", source_backend="ch")
    assert resolved.target_type == "BYTEA"
    assert resolved.convert("é\x00text") == "é\x00text".encode()
    assert resolved.convert("normal") == b"normal"
    profile = ColumnProfile()
    profile.add("normal")
    assert (
        resolve_profile(profile, "gp", source_type="String", source_backend="ch").target_type
        == "TEXT"
    )
    profile.add("\ud800")
    with pytest.raises(ValueError, match="encoded exactly"):
        resolve_profile(profile, "gp", source_type="String", source_backend="ch")


@pytest.mark.parametrize(("backend", "native"), [("gp", "BYTEA"), ("trino", "VARBINARY")])
def test_mixed_decoded_and_raw_clickhouse_strings_preserve_native_bytes(backend, native):
    profile = ColumnProfile()
    profile.add("é")
    profile.add(b"\xff")
    resolved = resolve_profile(profile, backend, source_type="String", source_backend="ch")
    assert resolved.target_type == native
    assert resolved.convert("é") == b"\xc3\xa9"
    assert resolved.convert(b"\xff") == b"\xff"


@pytest.mark.parametrize("backend", ["gp", "trino"])
def test_heterogeneous_python_strings_and_bytes_keep_typed_fallback(backend):
    profile = ColumnProfile()
    profile.add("foo")
    profile.add(b"foo")
    resolved = resolve_profile(profile, backend)
    assert resolved.text_fallback
    assert resolved.convert("foo") == "foo"
    assert resolved.convert(b"foo") == "base64:Zm9v"


@pytest.mark.parametrize(("backend", "native"), [("gp", "BYTEA"), ("trino", "VARBINARY")])
def test_authoritative_binary_accepts_physical_clickhouse_bytes_not_unrelated_dataframe_strings(
    backend, native
):
    profile = ColumnProfile()
    profile.add("é")
    profile.add(b"\xff")
    resolved = resolve_profile(
        profile, backend, authoritative_type=native, source_type="String", source_backend="ch"
    )
    assert resolved.convert("é") == b"\xc3\xa9"
    assert resolved.convert(b"\xff") == b"\xff"
    with pytest.raises(ValueError, match="authoritative binary"):
        resolve_profile(profile, backend, authoritative_type=native)
    profile.add("\ud800")
    with pytest.raises(ValueError, match="authoritative binary"):
        resolve_profile(
            profile, backend, authoritative_type=native, source_type="String", source_backend="ch"
        )


@pytest.mark.parametrize("value", [1, b"binary", [1], {"id": 1}])
def test_authoritative_uuid_rejects_other_native_families(value):
    profile = ColumnProfile()
    profile.add(value)
    with pytest.raises(ValueError, match="authoritative UUID"):
        validate_profile(profile, "UUID", "ch")


def test_authoritative_uuid_keeps_native_objects_and_parses_valid_text():
    from uuid import UUID  # noqa: PLC0415 - UUID family contract

    for value in [UUID(int=1), "00000000-0000-0000-0000-000000000001"]:
        profile = ColumnProfile()
        profile.add(value)
        resolved = resolve_profile(profile, "ch", authoritative_type="UUID")
        assert resolved.convert(value) == UUID(int=1)
