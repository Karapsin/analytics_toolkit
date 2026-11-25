from enum import Enum

import pytest
from analytics_toolkit.sql.backends.ch.value_validation import (
    enum_members,
    validate_constrained_value,
)
from analytics_toolkit.sql.backends.type_resolution import ColumnProfile, resolve_profile


@pytest.mark.parametrize(
    ("native", "values"),
    [("FixedString(3)", ["a", "éé"]), ("Enum8('ok' = 1, 'no' = -1)", ["ok", "bad"])],
)
def test_authoritative_constrained_last_value_rejected_before_certification(native, values):
    profile = ColumnProfile(native_type=native)
    for value in values:
        profile.observe(value)
    with pytest.raises(ValueError, match="authoritative"):
        resolve_profile(profile, "ch", authoritative_type=native)


def test_constrained_scalar_exact_native_validation():
    assert validate_constrained_value("é", "FixedString(2)") == "é"
    assert validate_constrained_value(memoryview(b"ab"), "FixedString(2)") == b"ab"
    for value in ["éé", 1, "\ud800"]:
        with pytest.raises(ValueError, match=r"FixedString|Enum"):
            validate_constrained_value(value, "FixedString(2)")
    for native in ["FixedString(0)", "FixedString(x)", "FixedString(2,3)"]:
        with pytest.raises(ValueError, match=r"FixedString|Enum"):
            validate_constrained_value("", native)
    native = "Enum16('ok' = 1, 'no' = -1)"

    class Status(Enum):
        OK = "ok"

    assert validate_constrained_value(Status.OK, native) == "ok"
    assert validate_constrained_value(-1, native) == -1
    for value in [True, "bad", 2]:
        with pytest.raises(ValueError, match="member"):
            validate_constrained_value(value, native)
    for malformed in [
        "Enum8('a'=128)",
        "Enum8('a'=1,'b'=1)",
        "Enum8('a'=1,'a'=2)",
        "Enum8(1=1)",
        "Enum8('a'=x)",
        "Enum8()",
    ]:
        with pytest.raises(ValueError, match=r"FixedString|Enum"):
            enum_members(malformed)
    profile = ColumnProfile()
    profile.observe("ok")
    resolved = resolve_profile(profile, "ch", authoritative_type=native)
    assert resolved.convert("ok") == "ok"
    with pytest.raises(ValueError, match="member"):
        resolved.convert("bad")


def test_unknown_authoritative_requires_source_domain_identity():
    profile = ColumnProfile()
    profile.observe(1)
    with pytest.raises(ValueError, match="Cannot certify"):
        resolve_profile(profile, "gp", authoritative_type='"amount_domain"')
    assert (
        resolve_profile(
            profile,
            "gp",
            authoritative_type='"amount_domain"',
            source_type='"amount_domain"',
            source_backend="gp",
        ).target_type
        == '"amount_domain"'
    )


def test_explicit_trino_picosecond_wrapper_reaches_exact_binding():
    from analytics_toolkit.sql.backends.type_resolution import (  # noqa: PLC0415 - regression
        ResolvedColumn,
    )
    from analytics_toolkit.sql.core.exact_values import ExactTemporal  # noqa: PLC0415 - regression

    value = ResolvedColumn("TIMESTAMP(12)", target_backend="trino").convert(ExactTemporal(1, 9))
    assert isinstance(value, ExactTemporal)
    assert value.ticks == 1
    assert value.scale == 9
    assert value.isoformat().endswith(".000000001")


def test_gp_character_varying_converts_exact_native_values_to_text():
    from decimal import Decimal  # noqa: PLC0415 - exact text regression

    from analytics_toolkit.sql.backends.type_resolution import (  # noqa: PLC0415 - regression
        ResolvedColumn,
    )
    from analytics_toolkit.sql.core.exact_values import ExactTemporal  # noqa: PLC0415 - regression

    conversion = ResolvedColumn("CHARACTER VARYING", target_backend="gp")
    assert (
        conversion.convert(Decimal("12345678901234567890.123456789"))
        == "12345678901234567890.123456789"
    )
    assert conversion.convert(ExactTemporal(1, 9)) == "1970-01-01T00:00:00.000000001"
