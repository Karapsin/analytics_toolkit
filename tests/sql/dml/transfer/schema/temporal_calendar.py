from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.core.exact_values import ExactTemporal

from tests.sql._support.adaptive_transfer import Scenario, install, track_spools
from tests.sql._support.load_table import load_df_module

_VALID_DATE = dt.date(2024, 1, 1)
_VALID_STAMP = dt.datetime.fromisoformat("2024-03-01T12:34:56.123456+05:30")
_CALENDAR_CASES = [
    ("DATE", [_VALID_DATE, dt.date(1800, 1, 1)], ["2024-01-01", "1800-01-01"]),
    ("DATE", [_VALID_DATE, dt.date(2300, 1, 1)], ["2024-01-01", "2300-01-01"]),
    (
        "TIMESTAMP WITH TIME ZONE",
        [_VALID_STAMP, dt.datetime.fromisoformat("1800-06-01T12:34:56.654321+05:30")],
        ["2024-03-01T07:04:56.123456+00:00", "1800-06-01T07:04:56.654321+00:00"],
    ),
    (
        "TIMESTAMP WITH TIME ZONE",
        [_VALID_STAMP, dt.datetime.fromisoformat("2300-06-01T12:34:56.654321+05:30")],
        ["2024-03-01T07:04:56.123456+00:00", "2300-06-01T07:04:56.654321+00:00"],
    ),
]


def gp_to_new_ch(
    monkeypatch: pytest.MonkeyPatch, native: str, values: list[Any], **kwargs: Any
) -> Scenario:
    return install(
        monkeypatch,
        native_type=native,
        rows=[(value,) for value in values],
        from_db_key="gp",
        from_db_backend="gp",
        to_db_key="ch",
        to_db_backend="ch",
        target_table="target.events",
        write_mode="append",
        replace_target_table=False,
        **kwargs,
    )


@pytest.mark.parametrize(("native", "values", "expected"), _CALENDAR_CASES)
def test_new_ch_transfer_profiles_the_last_calendar_extreme_and_freezes_exact_text(
    monkeypatch: pytest.MonkeyPatch, native: str, values: list[Any], expected: list[str]
) -> None:
    scenario = gp_to_new_ch(monkeypatch, native, values)
    paths = track_spools(monkeypatch)

    result = scenario.run(metadata=True)

    assert result.rows == len(values)
    assert result.metadata.input_count == len(values)
    assert scenario.schemas == [{"id": "String"}]
    assert scenario.loaded == [[(value,) for value in expected]]
    assert scenario.read_count == 1
    assert scenario.events.index("complete:SELECT id FROM source") < scenario.events.index(
        "create_target"
    )
    assert paths
    assert all(not path.exists() for path in paths)


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize(
    ("native", "values", "_expected"), [_CALENDAR_CASES[0], _CALENDAR_CASES[3]]
)
def test_authoritative_ch_calendar_schema_rejects_extremes_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
    existing: bool,
    native: str,
    values: list[Any],
    _expected: list[str],
) -> None:
    target = "Date32" if native == "DATE" else "DateTime64(6, 'UTC')"
    scenario = gp_to_new_ch(
        monkeypatch, native, values, table_schema=None if existing else {"id": target}
    )
    if existing:
        scenario.existing_types = {"id": target}

    with pytest.raises(ValueError, match=r"authoritative (date|timestamp) range"):
        scenario.run()

    assert "create_target" not in scenario.events
    assert not scenario.loaded
    assert scenario.finalizations == 0


def patch_load_boundaries(monkeypatch: pytest.MonkeyPatch, existing_type: str | None = None):
    adapter = get_backend_adapter("ch")
    connection = SimpleNamespace(close=lambda: None)
    created = []
    loaded = []
    monkeypatch.setattr(load_df_module, "get_sql_connection", lambda _key: connection)
    monkeypatch.setattr(load_df_module, "table_exists", lambda *_a, **_k: existing_type is not None)

    def create(_backend, _connection, _table, frame, **kwargs):
        schema = kwargs.get("table_schema") or {
            name: adapter.infer_dataframe_column_type(frame[name]) for name in frame.columns
        }
        created.append(schema)

    def load(**kwargs):
        loaded.append(kwargs["df"]["id"].tolist())
        return len(kwargs["df"])

    monkeypatch.setattr(load_df_module, "_create_sql_table_with_connection", create)
    monkeypatch.setattr(
        load_df_module,
        "get_table_column_types",
        lambda *_a, **_k: {"id": existing_type} if existing_type else created[-1],
    )
    monkeypatch.setattr(load_df_module, "_load_dataframe", load)
    monkeypatch.setattr(load_df_module, "analyze_table", lambda *_a, **_k: None)
    return created, loaded


def load_ch(values: list[Any], **kwargs: Any):
    return sql.load_df(
        "ch",
        "target.events",
        pd.DataFrame({"id": pd.Series(values, dtype=object)}),
        write_mode="append",
        ch_distributed_table=False,
        ch_engine="MergeTree",
        order_by="id",
        retry_cnt=1,
        timeout_increment=0,
        **kwargs,
    )


@pytest.mark.parametrize(("_native", "values", "expected"), _CALENDAR_CASES)
def test_new_ch_dataframe_load_uses_exact_text_for_the_whole_calendar_column(
    monkeypatch: pytest.MonkeyPatch, _native: str, values: list[Any], expected: list[str]
) -> None:
    created, loaded = patch_load_boundaries(monkeypatch)

    assert load_ch(values) == len(values)

    assert created == [{"id": "String"}]
    assert loaded == [expected]


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize(
    ("native", "values", "_expected"), [_CALENDAR_CASES[0], _CALENDAR_CASES[3]]
)
def test_ch_dataframe_authority_rejects_calendar_overflow_before_ddl_or_insert(
    monkeypatch: pytest.MonkeyPatch,
    existing: bool,
    native: str,
    values: list[Any],
    _expected: list[str],
) -> None:
    target = "Date32" if native == "DATE" else "DateTime64(6, 'UTC')"
    created, loaded = patch_load_boundaries(monkeypatch, target if existing else None)

    with pytest.raises(ValueError, match=r"authoritative (date|timestamp) range"):
        load_ch(values, table_schema=None if existing else {"id": target})

    assert not created
    assert not loaded


@pytest.mark.parametrize("nanoseconds", [123456000, 123456001])
def test_future_trino_timestamp_is_native_only_when_lower_precision_is_exact(
    monkeypatch: pytest.MonkeyPatch, nanoseconds: int
) -> None:
    ticks = (dt.date(2263, 1, 1) - dt.date(1970, 1, 1)).days * 86400 * 10**9 + nanoseconds
    value = ExactTemporal(ticks, 9)
    scenario = install(
        monkeypatch,
        native_type="TIMESTAMP(9)",
        rows=[(value,)],
        from_db_key="trino",
        from_db_backend="trino",
        to_db_key="ch",
        to_db_backend="ch",
    )
    if nanoseconds == 123456000:
        expected_type = "DateTime64(6)"
        expected = dt.datetime.fromisoformat("2263-01-01T00:00:00.123456")
    else:
        expected_type = "String"
        expected = "2263-01-01T00:00:00.123456001"

    assert scenario.run() == 1
    assert scenario.schemas == [{"id": expected_type}]
    assert scenario.loaded == [[(expected,)]]
    assert scenario.read_count == 1


@pytest.mark.parametrize("nanoseconds", [123456000, 123456001])
def test_future_dataframe_timestamp_does_not_narrow_unaligned_ticks(
    monkeypatch: pytest.MonkeyPatch, nanoseconds: int
) -> None:
    ticks = (dt.date(2263, 1, 1) - dt.date(1970, 1, 1)).days * 86400 * 10**9 + nanoseconds
    value = ExactTemporal(ticks, 9)
    created, loaded = patch_load_boundaries(monkeypatch)

    assert load_ch([value]) == 1

    if nanoseconds == 123456000:
        assert created == [{"id": "DateTime64(6)"}]
        assert loaded == [[dt.datetime.fromisoformat("2263-01-01T00:00:00.123456")]]
    else:
        assert created == [{"id": "String"}]
        assert loaded == [["2263-01-01T00:00:00.123456001"]]


def test_future_gp_microsecond_timestamp_remains_native_in_transfer_and_dataframe_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = dt.datetime.fromisoformat("2263-01-01T00:00:00.123456")
    scenario = gp_to_new_ch(monkeypatch, "TIMESTAMP(6)", [value])

    assert scenario.run() == 1
    assert scenario.schemas == [{"id": "DateTime64(6)"}]
    assert scenario.loaded == [[(value,)]]
    assert scenario.read_count == 1

    created, loaded = patch_load_boundaries(monkeypatch)
    assert load_ch([value]) == 1
    assert created == [{"id": "DateTime64(6)"}]
    assert loaded == [[value]]
