"""Native numeric text is certified before profile validation or publication."""

from __future__ import annotations

import datetime as dt
import struct
from copy import deepcopy
from decimal import Decimal, localcontext

import pytest
from analytics_toolkit.sql.backends.trino import numeric_text as numeric_text_codec
from analytics_toolkit.sql.backends.type_resolution import ColumnProfile, validate_profile
from analytics_toolkit.sql.core.exact_values import OrderedMapPairs


@pytest.fixture
def numeric_text():
    return numeric_text_codec


@pytest.mark.parametrize(
    ("native", "text", "expected"),
    [
        ("DECIMAL(6,2)", "123.50", Decimal("123.50")),
        ("DECIMAL(4,2)", "+1.25", Decimal("1.25")),
        ("DECIMAL(4,2)", ".5", Decimal(".5")),
        ("DECIMAL(4,0)", "1.", Decimal(1)),
        ("DECIMAL(5,0)", "1e2", Decimal("1E2")),
        ("DECIMAL(5,2)", "1.2300", Decimal("1.23")),
        ("DECIMAL(4,2)", "-0.00", Decimal("-0.00")),
        ("DECIMAL(4,1)", "\uff11\uff12.\uff15", Decimal("12.5")),
    ],
)
def test_trino_numeric_text_decimal_casts_preserve_exact_native_values(
    numeric_text, native, text, expected
):
    result = numeric_text.numeric_text_value(text, native)

    assert type(result) is Decimal
    assert result.as_tuple() == expected.as_tuple()
    assert numeric_text.convert_numeric_text(text, native) == (True, result)
    assert numeric_text.native_text_cast_value(text, native) == result


def test_trino_numeric_text_decimal_precision_is_independent_of_context(numeric_text):
    text = "12345678901234567890123456789.123456789"
    expected = Decimal(text)

    with localcontext() as context:
        context.prec = 2
        result = numeric_text.numeric_text_value(text, "DECIMAL(38,9)")

    assert type(result) is Decimal
    assert result.as_tuple() == expected.as_tuple()


@pytest.mark.parametrize("native", ["REAL", "DOUBLE", "DOUBLE PRECISION"])
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1.25", 1.25),
        ("-.5", -0.5),
        ("+1e2", 100.0),
        ("\x00\t1.25\x1f ", 1.25),
        ("1.25f", 1.25),
        ("1.25D", 1.25),
        ("0x1.4p0", 1.25),
        ("0x1p+4", 16.0),
        ("-0x2p+2", -8.0),
        ("-0X.8P+1F", -1.0),
        ("1e-1000", 0.0),
    ],
)
def test_trino_numeric_text_floating_casts_follow_native_java_forms(
    numeric_text, native, text, expected
):
    result = numeric_text.numeric_text_value(text, native)

    assert type(result) is float
    assert result == expected


@pytest.mark.parametrize(
    ("text", "wire"),
    [
        ("1.0000000596046448", b"\x3f\x80\x00\x01"),
        ("1.000000059604644775390625", b"\x3f\x80\x00\x00"),
        ("1.000000178813934326171875", b"\x3f\x80\x00\x02"),
        ("-1.0000000596046448", b"\xbf\x80\x00\x01"),
        ("1e-45", b"\x00\x00\x00\x01"),
        ("-1e-1000", b"\x80\x00\x00\x00"),
    ],
)
def test_trino_real_text_rounds_once_at_native_single_precision(numeric_text, text, wire):
    result = numeric_text.numeric_text_value(text, "REAL")

    assert struct.pack(">f", result) == wire


@pytest.mark.parametrize("native", ["REAL", "DOUBLE"])
@pytest.mark.parametrize(
    ("text", "expected_bits"),
    [("0x1p-2000", b"\x00" * 8), ("-0x1p-2000", b"\x80" + b"\x00" * 7)],
)
def test_trino_hex_underflow_keeps_native_floating_zero_and_its_sign(
    numeric_text, native, text, expected_bits
):
    result = numeric_text.numeric_text_value(text, native)

    assert type(result) is float
    assert struct.pack(">d", result) == expected_bits
    assert numeric_text.native_text_cast_value(text, native) == result


@pytest.mark.parametrize(
    ("native", "text"),
    [
        ("VARCHAR", "private-numeric-unsupported"),
        ("DOUBLE", "private-numeric-983"),
        ("DOUBLE", "NaN"),
        ("DOUBLE", "+Infinity"),
        ("DOUBLE", "1_2.5"),
        ("DOUBLE", "\u00a01.25"),
        ("DOUBLE", "\u0661.\u0662\u0665"),
        ("DOUBLE", "1e400"),
        ("REAL", "3.4028236e38"),
        ("DECIMAL(6,2)", "NaN"),
        ("DECIMAL(6,2)", "Infinity"),
        ("DECIMAL(6,2)", " 123.50"),
        ("DECIMAL(6,2)", "123.50\n"),
        ("DECIMAL(6,2)", "1_2.50"),
        ("DECIMAL(6,2)", "\U0001d7da.5"),
        ("DECIMAL(4,2)", "100.00"),
        ("DECIMAL(4,2)", "1.234"),
    ],
)
def test_trino_numeric_text_rejects_invalid_nonfinite_or_inexact_inputs_privately(
    numeric_text, native, text
):
    with pytest.raises(
        numeric_text.TrinoNumericTextValueError, match="finite native numeric"
    ) as error:
        numeric_text.numeric_text_value(text, native)

    assert text not in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.analytics_toolkit_sql_retry_safe is False
    assert "CAST the source expression" in str(error.value)


@pytest.mark.parametrize("native", ["REAL", "DOUBLE", "DECIMAL(6,2)"])
@pytest.mark.parametrize("value", [None, 1, 1.25, Decimal("1.25")])
def test_trino_numeric_text_normalization_leaves_nonstring_paths_identical(
    numeric_text, native, value
):
    assert numeric_text.normalize_authoritative_numeric_text(value, native) is value
    handled, result = numeric_text.convert_numeric_text(value, native)
    assert handled is False
    assert result is value


@pytest.mark.parametrize("native", [None, "VARCHAR", "BIGINT", "BOOLEAN", "DATE"])
def test_trino_numeric_text_unrelated_schema_is_not_automatically_numeric(numeric_text, native):
    value = "1.25"

    assert numeric_text.normalize_authoritative_numeric_text(value, native) is value
    if native:
        assert numeric_text.convert_numeric_text(value, native) == (False, value)


def test_trino_numeric_text_native_nested_values_keep_the_existing_path(numeric_text):
    values = [(Decimal("123.50"), False), (None, None)]

    assert (
        numeric_text.normalize_authoritative_numeric_text(
            values, "ARRAY(ROW(amount DECIMAL(6,2), flag BOOLEAN))"
        )
        is values
    )


def test_trino_numeric_text_type_only_hook_does_not_admit_raw_string_profiles(numeric_text):
    profile = ColumnProfile()
    profile.add("1.25")
    assert numeric_text.validate_numeric_text_type_profile("DOUBLE") is False

    with pytest.raises(ValueError, match="floating-point domain"):
        validate_profile(profile, "DOUBLE", "trino")


@pytest.mark.parametrize(
    "native",
    [
        "DOUBLE(3)",
        "REAL()",
        "Nullable(DOUBLE)",
        "DECIMAL()",
        "DECIMAL(39,2)",
        "ARRAY(REAL(3))",
        "Nullable(DECIMAL(6,2))",
        "LowCardinality(DECIMAL(6,2))",
    ],
)
def test_trino_numeric_text_type_declarations_reject_even_without_values(numeric_text, native):
    with pytest.raises(ValueError, match=r"(?:Trino numeric text|Authoritative decimal type)"):
        numeric_text.validate_numeric_text_type_profile(native)


def test_trino_numeric_text_nested_view_changes_only_declared_numeric_text(numeric_text):
    when = dt.datetime.fromisoformat("2024-02-29T12:00:00")
    values = [("123.50", "false", when, "42"), (None, None, None, None)]
    before = deepcopy(values)

    result = numeric_text.normalize_authoritative_numeric_text(
        values, "ARRAY(ROW(amount DECIMAL(6,2), flag BOOLEAN, seen TIMESTAMP(9), id BIGINT))"
    )

    assert result == [
        (Decimal("123.50"), "false", when, "42"),
        (None, None, None, None),
    ]
    assert result[0][2] is when
    assert values == before
    assert result is not values


def test_trino_numeric_text_map_key_collisions_reject_before_dictionary_loss(numeric_text):
    values = {"1.00": "first", "1": "second"}
    before = list(values.items())
    native = "MAP(DECIMAL(4,2), VARCHAR)"
    view = numeric_text.normalize_authoritative_numeric_text(values, native)
    profile = ColumnProfile(native_type=native, source_backend="trino")
    profile.add(view)

    assert isinstance(view, OrderedMapPairs)
    assert len(list(view.items())) == 2
    assert profile.map_duplicate_keys
    with pytest.raises(
        ValueError, match="cannot preserve its keys in the authoritative map schema"
    ):
        validate_profile(profile, native, "trino")

    assert list(values.items()) == before
    assert len(values) == 2


def test_trino_main_scalar_view_keeps_unrelated_timestamp_without_numeric_text(numeric_text):
    when = dt.datetime.fromisoformat("2024-02-29T12:34:56.123456")
    values = [("42", "false", "2024-02-29", when)]
    before = deepcopy(values)

    result = numeric_text.normalize_authoritative_value(
        values, "ARRAY(ROW(id BIGINT, flag BOOLEAN, day DATE, seen TIMESTAMP(9)))"
    )

    assert result == [(42, False, dt.date(2024, 2, 29), when)]
    assert result[0][3] is when
    assert values == before


def test_trino_main_view_preserves_all_native_container_identity(numeric_text):
    when = dt.datetime.fromisoformat("2024-02-29T12:34:56.123456")
    values = [(42, False, dt.date(2024, 2, 29), when)]

    assert (
        numeric_text.normalize_authoritative_value(
            values, "ARRAY(ROW(id BIGINT, flag BOOLEAN, day DATE, seen TIMESTAMP(9)))"
        )
        is values
    )


def test_trino_main_view_without_authoritative_type_preserves_raw_source_identity(numeric_text):
    values = {"amount": "1.25", "native": [Decimal("1.25"), bytearray(b"payload")]}
    before = deepcopy(values)
    native_values = values["native"]

    result = numeric_text.normalize_authoritative_value(values, None)

    assert result is values
    assert result["native"] is native_values
    assert result["native"][1] is native_values[1]
    assert type(result["amount"]) is str
    assert values == before
