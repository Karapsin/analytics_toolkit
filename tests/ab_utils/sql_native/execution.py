from __future__ import annotations

from tests.ab_utils._support.sql_native import (
    Any,
    _base_stats_from_expected,
    _build_sql_native_bootstrap_query,
    _group_frame,
    _install_sql_native_fakes,
    _metric_df,
    _table_info,
    _validation_frame,
    ab_utils,
    math,
    pd,
    pytest,
    re,
    sql_native,
)


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
