"""Native Greenplum BOOLEAN text casts preserve false values before publication."""

from __future__ import annotations

from copy import deepcopy

import numpy as np
import pandas as pd
import pytest
from analytics_toolkit import sql

from tests.sql._support.gp_booleans import boolean_target


def _load(frame, *, explicit):
    options = {"table_schema": {"payload": "BOOLEAN"}} if explicit else {}
    return sql.load_df(
        "gp",
        "sandbox.events",
        frame,
        write_mode="truncate_insert",
        retry_cnt=1,
        timeout_increment=0,
        **options,
    )


def _assert_native_boolean_load(monkeypatch, values, expected, *, explicit):
    database = boolean_target(monkeypatch)
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=object)})
    original = frame.copy(deep=True)

    assert _load(frame, explicit=explicit) == len(values)

    assert database.target.schema == {"payload": "BOOLEAN"}
    assert database.target.rows == [{"payload": value} for value in expected]
    assert all(row["payload"] is value for row, value in zip(database.target.rows, expected))
    assert database.bound_rows == [(value,) for value in expected]
    assert all(row[0] is value for row, value in zip(database.bound_rows, expected))
    wire = [
        ("NULL" if value is None else "TRUE" if value is True else "FALSE",) for value in expected
    ]
    assert database.wire_rows == wire
    assert [row for _template, row, _wire in database.formatter_observations] == [
        (value,) for value in expected
    ]
    assert [kind for kind, _statement in database.submissions] == ["CLEAR", "INSERT"]
    assert set(database.tables) == {("sandbox", "events")}
    pd.testing.assert_frame_equal(frame, original)
    assert database.opened
    assert all(connection.close_calls == 1 for connection in database.opened)


@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([None, False, True], [None, False, True]),
        ([np.bool_(False), np.bool_(True), None], [False, True, None]),
        (["true", "false", None], [True, False, None]),
        ([False, "false", True, "true", None], [False, False, True, True, None]),
        ([None, None], [None, None]),
    ],
    ids=["native", "numpy-native", "canonical-text", "mixed", "all-null"],
)
def test_gp_boolean_load_preserves_false_values_through_real_psycopg_formatter(
    monkeypatch, explicit, values, expected
):
    _assert_native_boolean_load(monkeypatch, values, expected, explicit=explicit)


@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
@pytest.mark.parametrize(
    "tokens",
    [
        ("TRUE", "FALSE"),
        (" \t\r\n\v\fTrUe \t\r\n\v\f", " \t\r\n\v\fFaLsE \t\r\n\v\f"),
        ("t", "f"),
        ("tr", "fa"),
        ("tru", "fal"),
        ("true", "fals"),
        ("y", "n"),
        ("ye", "no"),
        ("yes", "no"),
        ("on", "of"),
        ("on", "off"),
        ("1", "0"),
    ],
    ids=[
        "case",
        "ascii-whitespace",
        "first-prefix",
        "two-letter-prefix",
        "three-letter-prefix",
        "four-letter-false-prefix",
        "yes-no-prefix",
        "yes-two-letter-prefix",
        "yes-no",
        "off-prefix",
        "on-off",
        "textual-digits",
    ],
)
def test_gp_boolean_text_input_preserves_postgresql_forms(monkeypatch, explicit, tokens):
    values = [tokens[0], tokens[1], None]

    _assert_native_boolean_load(monkeypatch, values, [True, False, None], explicit=explicit)


@pytest.mark.parametrize("explicit", [False, True], ids=["existing", "explicit"])
@pytest.mark.parametrize(
    "bad",
    [
        "o",
        "",
        "truex",
        "falsetto",
        "\u00a0true",
        "t r u e",
        "true\x00",
        "01",
        "private-boolean-value",
    ],
    ids=[
        "ambiguous-prefix",
        "empty",
        "overlong-true",
        "overlong-false",
        "non-ascii",
        "interior-whitespace",
        "nul",
        "overlong-digit",
        "private-invalid",
    ],
)
def test_gp_invalid_late_boolean_rejects_before_target_clear(monkeypatch, explicit, bad):
    database = boolean_target(monkeypatch)
    target = database.target
    before = deepcopy(target.rows)
    frame = pd.DataFrame({"payload": pd.Series(["true", "false", None, bad], dtype=object)})
    original = frame.copy(deep=True)

    with pytest.raises(ValueError, match=r"bool|BOOLEAN") as error:
        _load(frame, explicit=explicit)

    assert "private-boolean-value" not in str(error.value)
    assert database.target is target
    assert database.target.rows == before
    assert database.target.schema == {"payload": "BOOLEAN"}
    assert database.live_mutations == []
    assert database.bound_rows == []
    assert database.wire_rows == []
    assert database.formatter_observations == []
    assert set(database.tables) == {("sandbox", "events")}
    pd.testing.assert_frame_equal(frame, original)
    assert all(connection.close_calls == 1 for connection in database.opened)
