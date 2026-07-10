from __future__ import annotations

import math
import re
import warnings
from types import SimpleNamespace
from typing import Any

import analytics_toolkit.ab_utils.metrics as metrics_module
import analytics_toolkit.ab_utils.parallel as parallel_module
import pandas as pd
import pytest
from analytics_toolkit import ab_utils
from analytics_toolkit.ab_utils import sql_native
from analytics_toolkit.ab_utils.sql_bootstrap import (
    _build_sql_native_bootstrap_query,
    _plan_sql_native_bootstrap_batches,
    _reduce_sql_native_bootstrap_batches,
)


def _metric_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "user_id": [1, 2, 3, 4, 5, 6, 7, 8],
            "group_name": [
                "control",
                "control",
                "control",
                "control",
                "test",
                "test",
                "test",
                "test",
            ],
            "orders": [1.0, 2.0, 3.0, 4.0, 2.0, 3.0, 4.0, 5.0],
            "clicks": [1.0, 2.0, 1.0, 3.0, 2.0, 3.0, 2.0, 4.0],
            "views": [10.0, 12.0, 8.0, 15.0, 11.0, 14.0, 9.0, 16.0],
        }
    )


def _table_info() -> SimpleNamespace:
    return SimpleNamespace(
        backend="gp",
        exists=True,
        table="mart.ab_source",
        resolved_table=None,
        columns={
            "user_id": "integer",
            "group_name": "text",
            "orders": "double precision",
            "clicks": "double precision",
            "views": "double precision",
        },
    )


def _validation_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "row_count": 8,
                "null_user_rows": 0,
                "null_group_rows": 0,
                "duplicate_user_rows": 0,
                "control_rows": 4,
                "non_control_group_count": 1,
            }
        ]
    )


def _group_frame() -> pd.DataFrame:
    return pd.DataFrame({"group_name": ["control", "test"]})


def _base_stats_from_expected(expected: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for _, row in expected.iterrows():
        rows.append(
            {
                "metric_name": row["metric_name"],
                "metric_type": row["metric_type"],
                "group_name": row["group_2"],
                "n": row["n0"],
                "metric_value": row["metric_control"],
                "variance_value": row["variance_control"],
                "outliers_cutoff": row["outliers_cutoff"],
                "outliers_n": row["outliers_n_control"],
            }
        )
        rows.append(
            {
                "metric_name": row["metric_name"],
                "metric_type": row["metric_type"],
                "group_name": row["group_1"],
                "n": row["n1"],
                "metric_value": row["metric_test"],
                "variance_value": row["variance_test"],
                "outliers_cutoff": row["outliers_cutoff"],
                "outliers_n": row["outliers_n_test"],
            }
        )
    return pd.DataFrame(rows)


def _install_sql_native_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    base_stats: pd.DataFrame,
    bootstrap_stats: pd.DataFrame | None = None,
    metadata_frame: pd.DataFrame | None = None,
) -> list[str]:
    queries: list[str] = []
    monkeypatch.setattr(
        sql_native.sql_facade, "table_info", lambda *_args, **_kwargs: _table_info()
    )

    def fake_connection_config(db_key: str) -> SimpleNamespace:
        return SimpleNamespace(connection_key=db_key, backend="gp")

    monkeypatch.setattr(sql_native, "get_connection_config", fake_connection_config)

    def fake_read(**kwargs: Any) -> pd.DataFrame:
        query = kwargs["query"]
        queries.append(query)
        if "analytics_toolkit_ab_sql_native_bootstrap" in query:
            return bootstrap_stats if bootstrap_stats is not None else pd.DataFrame()
        if "WHERE 1 = 0" in query:
            return metadata_frame if metadata_frame is not None else pd.DataFrame()
        if "duplicate_user_rows" in query:
            return _validation_frame()
        if query.lstrip().startswith("SELECT DISTINCT"):
            return _group_frame()
        return base_stats

    monkeypatch.setattr(sql_native, "_read_sql_native_query", fake_read)
    return queries


def _install_sql_backed_dataframe_fakes(
    monkeypatch: pytest.MonkeyPatch,
    df: pd.DataFrame,
) -> list[list[dict[str, Any]]]:
    task_batches: list[list[dict[str, Any]]] = []

    def fake_async_sql(
        tasks: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, pd.DataFrame]:
        assert kwargs["concurrency"] == 1
        assert kwargs["fail_fast"] is True
        assert kwargs["progress"] is False
        task_batches.append(tasks)
        return {str(task["name"]): df.copy() for task in tasks}

    monkeypatch.setattr(parallel_module, "async_sql", fake_async_sql)
    return task_batches


def test_compute_test_metrics_sql_native_is_exported() -> None:
    assert ab_utils.compute_test_metrics_sql_native is sql_native.compute_test_metrics_sql_native
    assert (
        metrics_module.compute_test_metrics_sql_native is sql_native.compute_test_metrics_sql_native
    )


def test_compute_test_metrics_sql_native_finalizes_compact_sql_stats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ratio_metrics = [{"name": "ctr", "numerator": "clicks", "denominator": "views"}]
    expected = ab_utils.compute_test_metrics(
        _metric_df(),
        ratio_metrics=ratio_metrics,
        test_vs_test=False,
        outliers_quantile=1,
    )
    queries = _install_sql_native_fakes(
        monkeypatch,
        base_stats=_base_stats_from_expected(expected),
    )

    result = ab_utils.compute_test_metrics_sql_native(
        "analytics",
        "mart.ab_source",
        ratio_metrics=ratio_metrics,
        test_vs_test=False,
        outliers_quantile=1,
    )

    pd.testing.assert_frame_equal(result, expected)
    assert any("duplicate_user_rows" in query for query in queries)
    assert not any("SELECT * FROM mart.ab_source" in query for query in queries)


def test_compute_test_metrics_sql_native_uses_bootstrap_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = ab_utils.compute_test_metrics(
        _metric_df()[["user_id", "group_name", "orders"]],
        test_vs_test=False,
        outliers_quantile=1,
    )
    bootstrap = pd.DataFrame(
        [
            {
                "metric_name": "orders",
                "group_1": "test",
                "group_2": "control",
                "se_bootstrap": 0.25,
                "bootstrap_adj_p": 0.125,
            }
        ]
    )
    _install_sql_native_fakes(
        monkeypatch,
        base_stats=_base_stats_from_expected(expected),
        bootstrap_stats=bootstrap,
    )

    result = ab_utils.compute_test_metrics_sql_native(
        "analytics",
        "mart.ab_source",
        metric_columns=["orders"],
        test_vs_test=False,
        outliers_quantile=1,
        multiple_comparisons_adjustment=True,
        multiple_comparisons_adjustment_resamples=10,
    )

    assert result.loc[0, "s.e. bootstrap"] == pytest.approx(0.25)
    assert result.loc[0, "bootstrap_adj_p"] == pytest.approx(0.125)


def test_sql_native_bootstrap_batches_large_sources_sequentially(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = ab_utils.compute_test_metrics(
        _metric_df()[["user_id", "group_name", "orders"]],
        test_vs_test=False,
        outliers_quantile=1,
    )
    queries: list[str] = []
    monkeypatch.setattr(
        sql_native.sql_facade, "table_info", lambda *_args, **_kwargs: _table_info()
    )

    def fake_read(**kwargs: Any) -> pd.DataFrame:
        query = kwargs["query"]
        queries.append(query)
        if "analytics_toolkit_ab_sql_native_bootstrap" in query:
            match = re.search(r"resamples=(\d+):(\d+)", query)
            assert match is not None
            batch_size = int(match.group(2)) - int(match.group(1)) + 1
            return pd.DataFrame(
                [
                    {
                        "metric_name": "orders",
                        "group_1": "test",
                        "group_2": "control",
                        "requested_resamples": batch_size,
                        "valid_family_resamples": batch_size,
                        "valid_delta_resamples": batch_size,
                        "delta_mean": 1.0,
                        "delta_m2": 0.0,
                        "max_t_exceedances": 0,
                    }
                ]
            )
        if "duplicate_user_rows" in query:
            validation = _validation_frame()
            validation.loc[0, "row_count"] = 100_000
            return validation
        if query.lstrip().startswith("SELECT DISTINCT"):
            return _group_frame()
        return _base_stats_from_expected(expected)

    monkeypatch.setattr(sql_native, "_read_sql_native_query", fake_read)

    with pytest.warns(RuntimeWarning, match="3 sequential queries"):
        result = ab_utils.compute_test_metrics_sql_native(
            "analytics",
            "mart.ab_source",
            metric_columns=["orders"],
            test_vs_test=False,
            outliers_quantile=1,
            multiple_comparisons_adjustment=True,
            multiple_comparisons_adjustment_resamples=25,
            bootstrap_random_state=None,
            bootstrap_large_source_row_threshold=100_000,
            bootstrap_large_source_resamples_per_query=10,
        )

    bootstrap_queries = [
        query for query in queries if "analytics_toolkit_ab_sql_native_bootstrap" in query
    ]
    assert [re.search(r"resamples=(\d+:\d+)", query).group(1) for query in bootstrap_queries] == [
        "1:10",
        "11:20",
        "21:25",
    ]
    assert len({re.search(r"seed=(\d+)", query).group(1) for query in bootstrap_queries}) == 1
    assert result.loc[0, "s.e. bootstrap"] == pytest.approx(0.0)
    assert result.loc[0, "bootstrap_adj_p"] == pytest.approx(1 / 26)


@pytest.mark.parametrize(
    ("backend", "generator", "hash_function", "finite_predicate", "null_literal"),
    [
        (
            "gp",
            "generate_series",
            "MD5",
            "CAST('NaN' AS DOUBLE PRECISION)",
            "CAST(NULL AS DOUBLE PRECISION)",
        ),
        (
            "trino",
            "UNNEST(sequence",
            "XXHASH64",
            "is_finite(t_star)",
            "CAST(NULL AS DOUBLE)",
        ),
        (
            "ch",
            "numbers(4)",
            "cityHash64",
            "isFinite(t_star)",
            "CAST(NULL AS Nullable(Float64))",
        ),
    ],
)
def test_sql_native_bootstrap_query_is_executable_compact_max_t_sql(
    backend: str,
    generator: str,
    hash_function: str,
    finite_predicate: str,
    null_literal: str,
) -> None:
    definitions = [
        {"kind": "mean", "metric_key": "orders", "column": "orders"},
        {
            "kind": "ratio",
            "metric_key": "ctr",
            "ratio_spec": {
                "numerator": "clicks",
                "denominator": "views",
                "level": "agg",
            },
        },
    ]
    observed = {
        ("orders", "test", "control"): (1.0, 0.5),
        ("ctr", "test", "control"): (math.nan, math.nan),
    }

    query = _build_sql_native_bootstrap_query(
        backend=backend,
        source_sql="mart.ab_source",
        sql_where="event_date >= DATE '2026-01-01'",
        group="group_name",
        user_id="user_id",
        comparisons=[("test", "control")],
        metric_definitions=definitions,
        outliers_quantile=0.99,
        outliers_policy="truncate",
        resamples=4,
        random_state=17,
        observed_statistics=observed,
    )

    assert generator in query
    assert hash_function in query
    assert finite_predicate in query
    assert null_literal in query
    assert "delta_star - observed_delta" in query
    assert "family_max_t" in query
    assert "valid_family_resamples" in query
    assert "delta_m2" in query
    assert "WHERE 1 = 0" not in query
    assert "SELECT * FROM mart.ab_source" not in query


def test_plan_sql_native_bootstrap_batches_uses_row_budget_and_large_cap() -> None:
    assert _plan_sql_native_bootstrap_batches(
        row_count=1_000,
        resamples=600,
        large_source_row_threshold=100_000,
        large_source_resamples_per_query=10,
    ) == [(1, 250), (251, 250), (501, 100)]
    assert _plan_sql_native_bootstrap_batches(
        row_count=100_000,
        resamples=25,
        large_source_row_threshold=100_000,
        large_source_resamples_per_query=10,
    ) == [(1, 10), (11, 10), (21, 5)]
    assert _plan_sql_native_bootstrap_batches(
        row_count=2_000_000,
        resamples=5,
        large_source_row_threshold=100_000,
        large_source_resamples_per_query=10,
    ) == [(1, 2), (3, 2), (5, 1)]


@pytest.mark.parametrize(
    ("overrides", "error", "message"),
    [
        ({"large_source_row_threshold": 0}, ValueError, "row_threshold must be positive"),
        (
            {"large_source_resamples_per_query": 1.5},
            TypeError,
            "resamples_per_query must be an integer",
        ),
        ({"row_count": True}, TypeError, "row_count must be an integer"),
        ({"row_count": 0}, ValueError, "row_count must be positive"),
        ({"resamples": 1.5}, TypeError, "resamples must be an integer"),
        ({"resamples": 0}, ValueError, "resamples must be positive"),
    ],
)
def test_plan_sql_native_bootstrap_batches_rejects_invalid_options(
    overrides: dict[str, object],
    error: type[Exception],
    message: str,
) -> None:
    kwargs: dict[str, object] = {
        "row_count": 100,
        "resamples": 10,
        "large_source_row_threshold": 100_000,
        "large_source_resamples_per_query": 10,
    }
    kwargs.update(overrides)

    with pytest.raises(error, match=message):
        _plan_sql_native_bootstrap_batches(**kwargs)


@pytest.mark.parametrize(
    ("overrides", "error", "message"),
    [
        ({"backend": "unknown"}, ValueError, "Unsupported SQL backend"),
        ({"resamples": True}, TypeError, "resamples must be an integer"),
        ({"resamples": 0}, ValueError, "resamples must be positive"),
        ({"resample_start": 1.5}, TypeError, "resample_start must be an integer"),
        ({"resample_start": 0}, ValueError, "resample_start must be positive"),
        ({"comparisons": []}, ValueError, "requires metrics and comparisons"),
        ({"metric_definitions": []}, ValueError, "requires metrics and comparisons"),
    ],
)
def test_sql_native_bootstrap_query_rejects_invalid_contract(
    overrides: dict[str, object],
    error: type[Exception],
    message: str,
) -> None:
    kwargs: dict[str, object] = {
        "backend": "gp",
        "source_sql": "mart.ab_source",
        "sql_where": None,
        "group": "group_name",
        "user_id": "user_id",
        "comparisons": [("test", "control")],
        "metric_definitions": [{"kind": "mean", "metric_key": "orders", "column": "orders"}],
        "outliers_quantile": 0.99,
        "outliers_policy": "truncate",
        "resamples": 2,
        "random_state": 0,
    }
    kwargs.update(overrides)

    with pytest.raises(error, match=message):
        _build_sql_native_bootstrap_query(**kwargs)


def test_sql_native_bootstrap_query_builds_user_ratio_values() -> None:
    query = _build_sql_native_bootstrap_query(
        backend="gp",
        source_sql="mart.ab_source",
        sql_where=None,
        group="group_name",
        user_id="user_id",
        comparisons=[("test", "control")],
        metric_definitions=[
            {
                "kind": "ratio",
                "metric_key": "ctr_user",
                "ratio_spec": {
                    "numerator": "clicks",
                    "denominator": "views",
                    "level": "user",
                },
            }
        ],
        outliers_quantile=1,
        outliers_policy="drop",
        resamples=2,
        random_state=0,
        observed_statistics={("ctr_user", "test", "control"): (0.1, 0.05)},
    )

    assert "metric_0_denominator > 0" in query
    assert "THEN NULL ELSE raw.value END" in query


def test_reduce_sql_native_bootstrap_batches_merges_moments_and_plus_one() -> None:
    columns = {
        "metric_name": "orders",
        "group_1": "test",
        "group_2": "control",
        "requested_resamples": 2,
        "valid_family_resamples": 2,
        "valid_delta_resamples": 2,
        "max_t_exceedances": 1,
    }
    first = pd.DataFrame([{**columns, "delta_mean": 2.0, "delta_m2": 2.0}])
    second = pd.DataFrame(
        [
            {
                **columns,
                "valid_family_resamples": 1,
                "delta_mean": 6.0,
                "delta_m2": 2.0,
            }
        ]
    )

    with pytest.warns(RuntimeWarning, match="discarded 1 of 4"):
        result = _reduce_sql_native_bootstrap_batches(
            batches=[(2, first), (2, second)],
            observed_statistics={("orders", "test", "control"): (1.0, 0.5)},
        )

    assert result.loc[0, "se_bootstrap"] == pytest.approx(math.sqrt(20 / 3))
    assert result.loc[0, "bootstrap_adj_p"] == pytest.approx(3 / 4)


def test_reduce_sql_native_bootstrap_batches_requires_every_expected_key() -> None:
    columns = [
        "metric_name",
        "group_1",
        "group_2",
        "requested_resamples",
        "valid_family_resamples",
        "valid_delta_resamples",
        "delta_mean",
        "delta_m2",
        "max_t_exceedances",
    ]

    with pytest.raises(ValueError, match="missing expected key"):
        _reduce_sql_native_bootstrap_batches(
            batches=[(2, pd.DataFrame(columns=columns))],
            observed_statistics={("orders", "test", "control"): (1.0, 0.5)},
        )


def _compact_bootstrap_summary(**overrides: object) -> pd.DataFrame:
    row: dict[str, object] = {
        "metric_name": "orders",
        "group_1": "test",
        "group_2": "control",
        "requested_resamples": 2,
        "valid_family_resamples": 2,
        "valid_delta_resamples": 2,
        "delta_mean": 1.0,
        "delta_m2": 0.5,
        "max_t_exceedances": 1,
    }
    row.update(overrides)
    return pd.DataFrame([row])


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("missing_column", "missing column"),
        ("duplicate", "duplicate key"),
        ("unexpected_key", "unexpected key"),
        ("requested_resamples", "requested_resamples is inconsistent"),
        ("valid_family_too_large", "counts are inconsistent"),
        ("exceedances_too_large", "counts are inconsistent"),
        ("negative_m2", "delta_m2 must be non-negative"),
        ("null_count", "count must not be null"),
        ("fractional_count", "count must be an integer"),
        ("negative_count", "count must be non-negative"),
        ("infinite_mean", "delta_mean must be finite"),
        ("nan_m2", "delta_m2 must be finite"),
    ],
)
def test_reduce_sql_native_bootstrap_rejects_malformed_summaries(
    case: str,
    message: str,
) -> None:
    if case == "missing_column":
        frame = _compact_bootstrap_summary().drop(columns=["delta_m2"])
    elif case == "duplicate":
        summary = _compact_bootstrap_summary()
        frame = pd.concat([summary, summary])
    else:
        overrides = {
            "unexpected_key": {"metric_name": "unexpected"},
            "requested_resamples": {"requested_resamples": 3},
            "valid_family_too_large": {"valid_family_resamples": 3},
            "exceedances_too_large": {"max_t_exceedances": 3},
            "negative_m2": {"delta_m2": -1},
            "null_count": {"valid_family_resamples": None},
            "fractional_count": {"valid_family_resamples": 1.5},
            "negative_count": {"valid_family_resamples": -1},
            "infinite_mean": {"delta_mean": math.inf},
            "nan_m2": {"delta_m2": math.nan},
        }
        frame = _compact_bootstrap_summary(**overrides[case])

    with pytest.raises(ValueError, match=message):
        _reduce_sql_native_bootstrap_batches(
            batches=[(2, frame)],
            observed_statistics={("orders", "test", "control"): (1.0, 0.5)},
        )


def test_reduce_sql_native_bootstrap_does_not_warn_without_observed_family() -> None:
    summary = pd.DataFrame(
        [
            {
                "metric_name": "orders",
                "group_1": "test",
                "group_2": "control",
                "requested_resamples": 2,
                "valid_family_resamples": 0,
                "valid_delta_resamples": 0,
                "delta_mean": None,
                "delta_m2": 0.0,
                "max_t_exceedances": 0,
            }
        ]
    )

    with warnings.catch_warnings(record=True) as caught:
        result = _reduce_sql_native_bootstrap_batches(
            batches=[(2, summary)],
            observed_statistics={("orders", "test", "control"): (math.nan, math.nan)},
        )

    assert caught == []
    assert math.isnan(result.loc[0, "bootstrap_adj_p"])


def test_sql_native_bootstrap_options_and_sequential_worker_are_validated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = ab_utils.compute_test_metrics(
        _metric_df()[["user_id", "group_name", "orders"]],
        test_vs_test=False,
        outliers_quantile=1,
    )
    _install_sql_native_fakes(
        monkeypatch,
        base_stats=_base_stats_from_expected(expected),
    )

    with pytest.raises(ValueError, match="bootstrap_n_jobs must be 1"):
        ab_utils.compute_test_metrics_sql_native(
            "analytics",
            "mart.ab_source",
            metric_columns=["orders"],
            bootstrap_n_jobs=2,
        )
    with pytest.raises(TypeError, match="row_threshold must be an integer"):
        ab_utils.compute_test_metrics_sql_native(
            "analytics",
            "mart.ab_source",
            metric_columns=["orders"],
            bootstrap_large_source_row_threshold=True,
        )
    with pytest.raises(ValueError, match="resamples_per_query must be positive"):
        ab_utils.compute_test_metrics_sql_native(
            "analytics",
            "mart.ab_source",
            metric_columns=["orders"],
            bootstrap_large_source_resamples_per_query=0,
        )


def test_compute_test_metrics_sql_native_accepts_raw_sql_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ratio_metrics: list[dict[str, object]] = []
    expected = ab_utils.compute_test_metrics(
        _metric_df()[["user_id", "group_name", "orders"]],
        test_vs_test=False,
        outliers_quantile=1,
    )
    metadata = pd.DataFrame(columns=["user_id", "group_name", "orders"])
    queries = _install_sql_native_fakes(
        monkeypatch,
        base_stats=_base_stats_from_expected(expected),
        metadata_frame=metadata,
    )

    result = ab_utils.compute_test_metrics_sql_native(
        "analytics",
        "select user_id, group_name, orders from mart.ab_source",
        source_type="sql",
        ratio_metrics=ratio_metrics,
        test_vs_test=False,
        outliers_quantile=1,
    )

    pd.testing.assert_frame_equal(result, expected)
    assert any("WHERE 1 = 0" in query for query in queries)


def test_compute_test_metrics_sql_native_task_mapping_adds_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = ab_utils.compute_test_metrics(
        _metric_df()[["user_id", "group_name", "orders"]],
        test_vs_test=False,
        outliers_quantile=1,
    )
    _install_sql_native_fakes(
        monkeypatch,
        base_stats=_base_stats_from_expected(expected),
    )

    result = ab_utils.compute_test_metrics_sql_native(
        "analytics",
        {
            "segment_a": {
                "source": "mart.ab_source",
                "metric_columns": ["orders"],
                "labels": {"segment": "a"},
                "test_vs_test": False,
                "outliers_quantile": 1,
            }
        },
    )

    assert isinstance(result, dict)
    assert result["segment_a"]["segment"].tolist() == ["a"]
    pd.testing.assert_frame_equal(
        result["segment_a"].drop(columns=["segment"]),
        expected,
    )


def test_metric_entrypoints_three_way_parity_single_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    df = _metric_df()
    ratio_metrics = [
        {
            "name": "ctr_agg",
            "numerator": "clicks",
            "denominator": "views",
            "level": "agg",
        },
        {
            "name": "ctr_user",
            "numerator": "clicks",
            "denominator": "views",
            "level": "user",
        },
    ]
    expected = ab_utils.compute_test_metrics(
        df,
        ratio_metrics=ratio_metrics,
        test_vs_test=False,
        outliers_quantile=1,
    )
    _install_sql_backed_dataframe_fakes(monkeypatch, df)
    _install_sql_native_fakes(
        monkeypatch,
        base_stats=_base_stats_from_expected(expected),
    )

    sql_backed_result = ab_utils.compute_metrics_from_sql(
        {"one": {"sql": "select * from mart.ab_source", "test_vs_test": False}},
        db_key="analytics",
        ratio_metrics=ratio_metrics,
        outliers_quantile=1,
        concurrency=1,
        progress=False,
    )
    sql_native_result = ab_utils.compute_test_metrics_sql_native(
        "analytics",
        "mart.ab_source",
        metric_columns=["orders", "clicks", "views"],
        ratio_metrics=ratio_metrics,
        test_vs_test=False,
        outliers_quantile=1,
    )

    pd.testing.assert_frame_equal(sql_backed_result["one"], expected)
    pd.testing.assert_frame_equal(sql_native_result, expected)
    assert list(sql_backed_result["one"].columns) == list(expected.columns)
    assert list(sql_native_result.columns) == list(expected.columns)


def test_metric_entrypoints_three_way_parity_task_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    df = _metric_df()
    ratio_metrics = [
        {
            "name": "ctr_agg",
            "numerator": "clicks",
            "denominator": "views",
            "level": "agg",
        },
        {
            "name": "ctr_user",
            "numerator": "clicks",
            "denominator": "views",
            "level": "user",
        },
    ]
    expected_tasks = ab_utils.compute_test_metrics(
        {
            "segment_a": {
                "df": df,
                "labels": {"segment": "a"},
                "ratio_metrics": ratio_metrics,
                "test_vs_test": False,
                "outliers_quantile": 1,
            }
        },
        concurrency=1,
        progress=False,
    )
    _install_sql_backed_dataframe_fakes(monkeypatch, df)
    _install_sql_native_fakes(
        monkeypatch,
        base_stats=_base_stats_from_expected(expected_tasks["segment_a"].drop(columns=["segment"])),
    )

    sql_backed_tasks = ab_utils.compute_metrics_from_sql(
        {
            "segment_a": {
                "sql": "select * from mart.ab_source",
                "labels": {"segment": "a"},
                "test_vs_test": False,
            }
        },
        db_key="analytics",
        ratio_metrics=ratio_metrics,
        outliers_quantile=1,
        concurrency=1,
        progress=False,
    )
    sql_native_tasks = ab_utils.compute_test_metrics_sql_native(
        "analytics",
        {
            "segment_a": {
                "source": "mart.ab_source",
                "metric_columns": ["orders", "clicks", "views"],
                "labels": {"segment": "a"},
                "ratio_metrics": ratio_metrics,
                "test_vs_test": False,
                "outliers_quantile": 1,
            }
        },
        concurrency=1,
        progress=False,
    )

    pd.testing.assert_frame_equal(sql_backed_tasks["segment_a"], expected_tasks["segment_a"])
    pd.testing.assert_frame_equal(sql_native_tasks["segment_a"], expected_tasks["segment_a"])
    assert list(sql_backed_tasks["segment_a"].columns) == list(expected_tasks["segment_a"].columns)
    assert list(sql_native_tasks["segment_a"].columns) == list(expected_tasks["segment_a"].columns)
