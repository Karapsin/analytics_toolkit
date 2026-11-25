"""Compatible native GP scalar values survive authoritative preflight."""

from __future__ import annotations

import ipaddress
import math
from copy import deepcopy
from decimal import Decimal

import pandas as pd
import pytest
from analytics_toolkit import sql
from psycopg2.extras import Inet

from tests.sql._support.gp_inet_casts import native_inet_target
from tests.sql._support.gp_native_scalars import native_scalar_target
from tests.sql._support.gp_numeric_text import _literal_text


def load(frame, native, explicit):
    return sql.load_df(
        "gp",
        "sandbox.events",
        frame,
        table_schema={"payload": native} if explicit else None,
        write_mode="truncate_insert",
        retry_cnt=1,
        timeout_increment=0,
    )


@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
def test_gp_native_integral_decimal_preserves_integer_domain_and_caller(monkeypatch, explicit):
    database = native_scalar_target(monkeypatch, "BIGINT")
    values = [Decimal("1.0"), Decimal(-(2**63)), Decimal(2**63 - 1), None]
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})
    assert load(frame, "BIGINT", explicit) == len(values)
    assert database.target.rows == [
        {"payload": int(value) if value is not None else None} for value in values
    ]
    assert frame.iloc[0, 0] is values[0]


@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
@pytest.mark.parametrize("bad", [Decimal("1.5"), Decimal(2**63)])
def test_gp_native_integer_rejects_fraction_or_overflow_before_clear(monkeypatch, explicit, bad):
    database = native_scalar_target(monkeypatch, "BIGINT")
    original = deepcopy(database.target.rows)
    frame = pd.DataFrame({"payload": pd.Series([Decimal("1.0"), bad], dtype=object)})
    with pytest.raises(ValueError, match=r"(?i)(integer|range|precision|native assignment)"):
        load(frame, "BIGINT", explicit)
    assert database.target.rows == original
    assert database.live_mutations == []


@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
@pytest.mark.parametrize("native", ["REAL", "DOUBLE PRECISION", "NUMERIC"])
def test_gp_native_special_text_stays_nonnull_and_preserves_sql_null(monkeypatch, explicit, native):
    database = native_scalar_target(monkeypatch, native)
    values = ["NaN", None] if native == "NUMERIC" else ["NaN", "Infinity", "-Infinity", None]
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})
    assert load(frame, native, explicit) == len(values)
    rows = [row["payload"] for row in database.target.rows]
    assert rows[0] is not None
    assert rows[0].is_nan() if native == "NUMERIC" else math.isnan(rows[0])
    assert rows[-1] is None
    if native != "NUMERIC":
        assert rows[1:3] == [float("inf"), -float("inf")]
    assert frame.iloc[0, 0] == "NaN"


@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
def test_gp_native_inet_wrapper_is_validated_without_mutating_caller(monkeypatch, explicit):
    database = native_inet_target(monkeypatch)
    value = Inet("192.0.2.1/24")
    frame = pd.DataFrame({"payload": pd.Series([value, None], dtype=object)})
    assert load(frame, "INET", explicit) == 2
    assert database.target.rows == [
        {"payload": ipaddress.ip_interface(value.addr)},
        {"payload": None},
    ]
    assert frame.iloc[0, 0] is value
    assert value.addr == "192.0.2.1/24"


def test_gp_numeric_infinity_is_decided_by_native_server_before_target_clear(monkeypatch):
    database = native_scalar_target(monkeypatch, "NUMERIC")
    original_rows = deepcopy(database.target.rows)
    decode = database.decode_value
    attempted = []

    def observe_native_input(node, native_type):
        attempted.append((native_type, _literal_text(node)))
        return decode(node, native_type)

    monkeypatch.setattr(database, "decode_value", observe_native_input)
    frame = pd.DataFrame({"payload": pd.Series(["Infinity"], dtype=object)})

    with pytest.raises(ValueError, match=r"(?i)native.*assignment"):
        load(frame, "NUMERIC", True)

    assert any(
        native in {"NUMERIC", "DECIMAL"} and text == "Infinity" for native, text in attempted
    )
    assert database.target.rows == original_rows
    assert database.live_mutations == []
    assert frame.iloc[0, 0] == "Infinity"
