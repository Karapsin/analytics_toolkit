from __future__ import annotations

from tests.ab_utils._support.edge_cases import (
    Any,
    SimpleNamespace,
    _mean_group_stats,
    _resolve_source_kwargs,
    _sql_source,
    _valid_sql_source_stats,
    math,
    pd,
    pytest,
    sql_native,
)


def test_sql_native_single_source_rejects_parallel_concurrency() -> None:
    with pytest.raises(ValueError, match="only when source is a task mapping"):
        sql_native.compute_test_metrics_sql_native(
            "analytics",
            "mart.ab_source",
            concurrency=2,
        )


def test_sql_native_single_rejects_cross_backend_pre_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = iter([_sql_source(backend="gp"), _sql_source(backend="trino")])
    monkeypatch.setattr(sql_native, "_resolve_sql_native_source", lambda **_kwargs: next(sources))

    with pytest.raises(ValueError, match="same backend"):
        sql_native.compute_test_metrics_sql_native(
            "analytics",
            "mart.ab_source",
            pre_exp_source="mart.pre_source",
            metric_columns=["orders"],
        )


def test_sql_native_single_requires_at_least_one_metric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sql_native,
        "_resolve_sql_native_source",
        lambda **_kwargs: _sql_source(columns=["user_id", "group_name"]),
    )

    with pytest.raises(ValueError, match="At least one metric"):
        sql_native.compute_test_metrics_sql_native("analytics", "mart.ab_source")


def test_resolve_sql_native_source_rejects_missing_and_empty_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="must not be None"):
        sql_native._resolve_sql_native_source(**_resolve_source_kwargs(source=None))
    with pytest.raises(ValueError, match="table name must not be empty"):
        sql_native._resolve_sql_native_source(**_resolve_source_kwargs(source=" "))

    monkeypatch.setattr(
        sql_native.sql_facade,
        "table_info",
        lambda *_args: SimpleNamespace(exists=False),
    )
    with pytest.raises(ValueError, match="does not exist"):
        sql_native._resolve_sql_native_source(**_resolve_source_kwargs())

    monkeypatch.setattr(
        sql_native,
        "get_connection_config",
        lambda _key: SimpleNamespace(backend="gp"),
    )
    with pytest.raises(ValueError, match="source SQL must not be empty"):
        sql_native._resolve_sql_native_source(
            **_resolve_source_kwargs(source=" ; ", source_type="sql")
        )


def test_sql_native_source_type_and_metric_column_validation() -> None:
    with pytest.raises(ValueError, match="either 'table' or 'sql'"):
        sql_native._normalize_source_type("view")

    with pytest.raises(ValueError, match="Missing required"):
        sql_native._resolve_metric_columns(
            columns=["user_id", "orders"],
            column_types={},
            metric_columns=None,
            ratio_specs=[],
            group="group_name",
            user_id="user_id",
        )
    with pytest.raises(ValueError, match="must not contain duplicates"):
        sql_native._resolve_metric_columns(
            columns=["user_id", "group_name", "orders"],
            column_types={},
            metric_columns=["orders", "orders"],
            ratio_specs=[],
            group="group_name",
            user_id="user_id",
        )
    with pytest.raises(ValueError, match="Missing metric"):
        sql_native._resolve_metric_columns(
            columns=["user_id", "group_name", "orders"],
            column_types={},
            metric_columns=["missing"],
            ratio_specs=[],
            group="group_name",
            user_id="user_id",
        )
    with pytest.raises(ValueError, match="Duplicate metric"):
        sql_native._validate_metric_name_conflicts(
            ["orders"],
            [{"name": "orders"}],
        )

    assert sql_native._is_sql_numeric_type("") is False
    assert sql_native._is_sql_numeric_type("Nullable(Int64)") is True


def test_read_sql_native_query_delegates_all_execution_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_read(**kwargs: object) -> pd.DataFrame:
        captured.update(kwargs)
        return pd.DataFrame({"value": [1]})

    monkeypatch.setattr(sql_native, "_read_sql_mde_query", fake_read)
    result = sql_native._read_sql_native_query(
        db_key="analytics",
        query="SELECT 1",
        print_queries=True,
        retry_cnt=2,
        timeout_increment=3.5,
        query_label="edge",
    )
    assert result.to_dict("list") == {"value": [1]}
    assert captured == {
        "db_key": "analytics",
        "query": "SELECT 1",
        "print_queries": True,
        "retry_cnt": 2,
        "timeout_increment": 3.5,
        "query_label": "edge",
    }


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("row_count", 0, "at least one row"),
        ("null_user_rows", 1, "must not contain missing"),
        ("duplicate_user_rows", 1, "must contain unique"),
        ("null_group_rows", 1, "must not contain missing"),
        ("control_rows", 0, "was not found"),
        ("non_control_group_count", 0, "non-control group"),
    ],
)
def test_validate_sql_native_source_stats_reports_each_contract_failure(
    field: str,
    value: int,
    message: str,
) -> None:
    stats = _valid_sql_source_stats()
    stats[field] = value
    with pytest.raises(ValueError, match=message):
        sql_native._validate_sql_native_source_stats(
            pd.DataFrame([stats]),
            group="group_name",
            control="control",
            user_id="user_id",
        )


def test_validate_sql_native_source_stats_rejects_empty_result() -> None:
    with pytest.raises(ValueError, match="returned no rows"):
        sql_native._validate_sql_native_source_stats(
            pd.DataFrame(),
            group="group_name",
            control="control",
            user_id="user_id",
        )


def test_read_sql_native_groups_requires_group_name_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sql_native,
        "_read_sql_native_query",
        lambda **_kwargs: pd.DataFrame({"wrong": ["control"]}),
    )
    with pytest.raises(ValueError, match="did not return group_name"):
        sql_native._read_sql_native_groups(
            db_key="analytics",
            backend="gp",
            source_sql='"mart"."ab_source"',
            sql_where=None,
            group="group_name",
            print_queries=False,
            retry_cnt=1,
            timeout_increment=1,
            query_label=None,
        )


def test_sql_native_cuped_query_builds_mean_and_ratio_comparisons() -> None:
    metric_definitions = [
        {"kind": "mean", "metric_key": "orders", "column": "orders"},
        {
            "kind": "ratio",
            "metric_key": "ctr",
            "ratio_spec": {
                "numerator": "clicks",
                "denominator": "views",
                "level": "user",
            },
        },
    ]
    query = sql_native._build_sql_native_cuped_query(
        backend="gp",
        source_sql='"mart"."ab_source"',
        sql_where="active",
        pre_source_sql='"mart"."pre_source"',
        pre_sql_where=None,
        group="group_name",
        user_id="user_id",
        comparisons=[("test_a", "control"), ("test_b", "control")],
        metric_definitions=metric_definitions,
        outliers_quantile=0.99,
        outliers_policy="truncate",
    )
    assert query.count("WITH exp_raw AS") == 4
    assert query.count("UNION ALL") == 3
    assert query.count("SELECT * FROM (\nWITH exp_raw AS") == 4
    assert "UNION ALL\nWITH" not in query
    assert "AS __analytics_toolkit_union_0003" in query
    assert "metric_pre" in query
    assert "test_a" in query
    assert "test_b" in query
    assert "clicks" in query
    assert "views" in query


@pytest.mark.parametrize("backend", ["gp", "trino", "ch"])
def test_sql_native_group_stats_wraps_ctes_before_union(backend: str) -> None:
    metric_definitions = [
        {"kind": "mean", "metric_key": "orders", "column": "orders"},
        {"kind": "mean", "metric_key": "revenue", "column": "revenue"},
    ]

    query = sql_native._build_sql_native_group_stats_query(
        backend=backend,
        source_sql='"mart"."ab_source"',
        sql_where=None,
        group="group_name",
        user_id="user_id",
        metric_definitions=metric_definitions,
        outliers_quantile=0.99,
        outliers_policy="truncate",
    )

    assert query.count("SELECT * FROM (\nWITH source AS") == 2
    assert query.count("UNION ALL") == 1
    assert "UNION ALL\nWITH" not in query
    assert "AS __analytics_toolkit_union_0000" in query
    assert "AS __analytics_toolkit_union_0001" in query


def test_sql_native_group_stats_keeps_single_cte_query_unwrapped() -> None:
    query = sql_native._build_sql_native_group_stats_query(
        backend="gp",
        source_sql='"mart"."ab_source"',
        sql_where=None,
        group="group_name",
        user_id="user_id",
        metric_definitions=[{"kind": "mean", "metric_key": "orders", "column": "orders"}],
        outliers_quantile=0.99,
        outliers_policy="truncate",
    )

    assert query.startswith("WITH source AS")
    assert "__analytics_toolkit_union_" not in query


def test_sql_native_greenplum_batches_union_queries_for_slice_limit() -> None:
    parts = [f"SELECT {index} AS value" for index in range(7)]

    batches = sql_native._batch_sql_native_union_queries(parts, backend="gp")

    assert len(batches) == 3
    assert [batch.count(" AS value") for batch in batches] == [3, 3, 1]
    assert [batch.count("UNION ALL") for batch in batches] == [2, 2, 0]


def test_sql_native_non_greenplum_keeps_union_query_together() -> None:
    parts = [f"SELECT {index} AS value" for index in range(7)]

    batches = sql_native._batch_sql_native_union_queries(parts, backend="trino")

    assert len(batches) == 1
    assert batches[0].count("UNION ALL") == 6


def test_sql_native_batch_helpers_preserve_empty_and_single_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert sql_native._batch_sql_native_union_queries([], backend="gp") == []
    empty = sql_native._read_sql_native_query_batches(
        db_key="analytics",
        queries=[],
        print_queries=False,
        retry_cnt=1,
        timeout_increment=1,
        query_label=None,
    )
    assert empty.empty

    frame = pd.DataFrame({"value": [1]})
    monkeypatch.setattr(sql_native, "_read_sql_native_query", lambda **_kwargs: frame)
    single = sql_native._read_sql_native_query_batches(
        db_key="analytics",
        queries=["query_1"],
        print_queries=False,
        retry_cnt=1,
        timeout_increment=1,
        query_label=None,
    )
    assert single is frame


def test_sql_native_cuped_query_builder_batches_rendered_parts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sql_native,
        "_build_sql_native_cuped_query_parts",
        lambda **_kwargs: ["SELECT 1", "SELECT 2"],
    )

    queries = sql_native._build_sql_native_cuped_queries(
        backend="trino",
        source_sql="source",
        sql_where=None,
        pre_source_sql="pre_source",
        pre_sql_where=None,
        group="group_name",
        user_id="user_id",
        comparisons=[("test", "control")],
        metric_definitions=[],
        outliers_quantile=0.99,
        outliers_policy="truncate",
    )

    assert len(queries) == 1
    assert "UNION ALL" in queries[0]


def test_read_sql_native_query_batches_concatenates_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sql_native,
        "_read_sql_native_query",
        lambda **kwargs: pd.DataFrame({"query": [kwargs["query"]]}),
    )

    result = sql_native._read_sql_native_query_batches(
        db_key="analytics",
        queries=["query_1", "query_2", "query_3"],
        print_queries=False,
        retry_cnt=1,
        timeout_increment=1,
        query_label=None,
    )

    assert result["query"].tolist() == ["query_1", "query_2", "query_3"]


def test_sql_native_observed_statistics_cover_aggregate_ratio_and_missing_groups() -> None:
    definition = {
        "kind": "ratio",
        "metric_key": "ctr",
        "ratio_spec": {
            "numerator": "clicks",
            "denominator": "views",
            "level": "agg",
        },
    }
    stats = pd.DataFrame(
        [
            {
                "metric_name": "ctr",
                "group_name": "control",
                "metric_value": 0.1,
                "variance_value": 0.01,
                "n": 10,
            },
            {
                "metric_name": "ctr",
                "group_name": "test",
                "metric_value": 0.2,
                "variance_value": 0.04,
                "n": 10,
            },
        ]
    )
    observed = sql_native._build_sql_native_observed_statistics(
        group_stats=stats,
        metric_definitions=[definition],
        comparisons=[("test", "control"), ("missing", "control")],
    )
    assert observed[("ctr", "test", "control")] == pytest.approx((0.1, math.sqrt(0.05)))
    assert all(math.isnan(value) for value in observed[("ctr", "missing", "control")])


def test_finalize_sql_native_result_fills_missing_cuped_and_bootstrap_fields() -> None:
    with pytest.warns(UserWarning, match="SQL CUPED stats are unavailable"):
        result = sql_native._finalize_sql_native_metric_result(
            group_stats=_mean_group_stats(),
            cuped_stats=None,
            bootstrap_stats=None,
            metric_definitions=[{"kind": "mean", "metric_key": "orders", "column": "orders"}],
            comparisons=[("test", "control")],
            mde_alpha=0.05,
            mde_power=0.8,
            include_cuped=True,
            include_bootstrap=True,
        )
    assert math.isnan(result.loc[0, "s.e. CUPED"])
    assert math.isnan(result.loc[0, "s.e. bootstrap"])
    assert math.isnan(result.loc[0, "bootstrap_adj_p"])


@pytest.mark.parametrize(
    ("cuped_row", "message"),
    [
        (None, "stats are unavailable"),
        (pd.Series({"pair_n": 0, "pre_var": 1.0}), "no overlapping"),
        (pd.Series({"pair_n": 2, "pre_var": 0.0}), "variance is not positive"),
        (
            pd.Series(
                {
                    "pair_n": 2,
                    "pre_var": 1.0,
                    "variance_group_2": 1.0,
                    "variance_group_1": 1.0,
                    "n_group_2": 1,
                    "n_group_1": 1,
                    "metric_group_2": 1.0,
                    "metric_group_1": 2.0,
                }
            ),
            "not enough overlapping",
        ),
    ],
)
def test_add_sql_native_cuped_fields_warns_for_unusable_summaries(
    cuped_row: pd.Series | None,
    message: str,
) -> None:
    row: dict[str, object] = {"metric_group_2": 1.0}
    with pytest.warns(UserWarning, match=message):
        sql_native._add_sql_native_cuped_fields(
            row=row,
            cuped_row=cuped_row,
            metric_name="orders",
            baseline_group="control",
            test_group="test",
            mde_alpha=0.05,
            mde_power=0.8,
        )
    assert math.isnan(float(row["s.e. CUPED"]))
    assert math.isnan(float(row["p-value CUPED"]))


def test_add_sql_native_cuped_fields_computes_valid_summary() -> None:
    row: dict[str, object] = {"metric_group_2": 1.0}
    sql_native._add_sql_native_cuped_fields(
        row=row,
        cuped_row=pd.Series(
            {
                "pair_n": 8,
                "pre_var": 1.0,
                "variance_group_2": 1.0,
                "variance_group_1": 1.0,
                "n_group_2": 4,
                "n_group_1": 4,
                "metric_group_2": 1.0,
                "metric_group_1": 2.0,
            }
        ),
        metric_name="orders",
        baseline_group="control",
        test_group="test",
        mde_alpha=0.05,
        mde_power=0.8,
    )
    assert row["s.e. CUPED"] == pytest.approx(math.sqrt(0.5))
    assert math.isfinite(float(row["p-value CUPED"]))
    assert math.isfinite(float(row["mde_abs CUPED"]))


def test_welch_summary_rejects_invalid_samples_and_zero_denominator() -> None:
    assert math.isnan(
        sql_native._compute_welch_p_value_from_summary(
            delta_abs=1.0,
            standard_error=math.nan,
            baseline_variance=1.0,
            baseline_n=4,
            test_variance=1.0,
            test_n=4,
        )
    )
    assert math.isnan(
        sql_native._compute_welch_p_value_from_summary(
            delta_abs=1.0,
            standard_error=1.0,
            baseline_variance=1.0,
            baseline_n=1,
            test_variance=1.0,
            test_n=4,
        )
    )
    assert math.isnan(
        sql_native._compute_welch_p_value_from_summary(
            delta_abs=1.0,
            standard_error=1.0,
            baseline_variance=0.0,
            baseline_n=4,
            test_variance=0.0,
            test_n=4,
        )
    )


def test_sql_native_task_runner_captures_errors_sequentially(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(_db_key: str, kwargs: Any, _labels: Any) -> pd.DataFrame:
        if kwargs["source"] == "bad":
            error = ValueError("bad source")
            raise error
        return pd.DataFrame({"value": [kwargs["source"]]})

    monkeypatch.setattr(sql_native, "_run_sql_native_task", fake_run)
    result = sql_native._compute_sql_native_metric_tasks(
        db_key="analytics",
        tasks={"good": {"source": "good"}, "bad": {"source": "bad"}},
        defaults={},
        concurrency=1,
        fail_fast=False,
        soft_concurrency_cap=None,
        hard_concurrency_cap=4,
        progress=False,
    )
    assert result["good"].to_dict("list") == {"value": ["good"]}
    assert result["bad"] == "bad source"

    with pytest.raises(ValueError, match="bad source"):
        sql_native._compute_sql_native_metric_tasks(
            db_key="analytics",
            tasks={"bad": {"source": "bad"}},
            defaults={},
            concurrency=1,
            fail_fast=True,
            soft_concurrency_cap=None,
            hard_concurrency_cap=4,
            progress=False,
        )


def test_sql_native_task_runner_handles_parallel_results_and_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(_db_key: str, kwargs: Any, _labels: Any) -> pd.DataFrame:
        if kwargs["source"] == "bad":
            error = ValueError("bad source")
            raise error
        return pd.DataFrame({"value": [kwargs["source"]]})

    monkeypatch.setattr(sql_native, "_run_sql_native_task", fake_run)
    result = sql_native._compute_sql_native_metric_tasks(
        db_key="analytics",
        tasks={"first": {"source": "one"}, "bad": {"source": "bad"}},
        defaults={},
        concurrency=2,
        fail_fast=False,
        soft_concurrency_cap=2,
        hard_concurrency_cap=2,
        progress=False,
    )
    assert list(result) == ["first", "bad"]
    assert result["first"].to_dict("list") == {"value": ["one"]}
    assert result["bad"] == "bad source"

    with pytest.raises(ValueError, match="bad source"):
        sql_native._compute_sql_native_metric_tasks(
            db_key="analytics",
            tasks={"first": {"source": "one"}, "bad": {"source": "bad"}},
            defaults={},
            concurrency=2,
            fail_fast=True,
            soft_concurrency_cap=2,
            hard_concurrency_cap=2,
            progress=False,
        )


def test_sql_native_task_runner_enforces_effective_hard_cap() -> None:
    with pytest.raises(ValueError, match="exceeds hard_concurrency_cap"):
        sql_native._compute_sql_native_metric_tasks(
            db_key="analytics",
            tasks={"one": {"source": "one"}},
            defaults={},
            concurrency=3,
            fail_fast=True,
            soft_concurrency_cap=3,
            hard_concurrency_cap=2,
            progress=False,
        )


@pytest.mark.parametrize(
    ("tasks", "error", "message"),
    [
        ([], TypeError, "non-empty mapping"),
        ({}, ValueError, "must not be empty"),
        ({1: {"source": "one"}}, ValueError, "non-empty strings"),
        ({"one": "bad"}, TypeError, "must be a mapping"),
        ({"one": {"source": "one", "unknown": 1}}, TypeError, "unexpected field"),
        ({"one": {"source": None}}, ValueError, "must define source"),
        ({"one": {"source": "one", "labels": []}}, TypeError, "labels must be a mapping"),
    ],
)
def test_validate_sql_native_tasks_rejects_invalid_mappings(
    tasks: object,
    error: type[Exception],
    message: str,
) -> None:
    with pytest.raises(error, match=message):
        sql_native._validate_sql_native_tasks(tasks, defaults={})


def test_validate_sql_native_tasks_normalizes_none_labels() -> None:
    assert sql_native._validate_sql_native_tasks(
        {"one": {"source": "one", "labels": None}},
        defaults={},
    ) == [("one", {"source": "one"}, {})]


def test_run_sql_native_task_handles_no_labels_and_label_conflicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = pd.DataFrame({"metric_name": ["orders"]})
    monkeypatch.setattr(
        sql_native,
        "_compute_test_metrics_sql_native_single",
        lambda **_kwargs: frame,
    )
    assert sql_native._run_sql_native_task("analytics", {}, {}) is frame
    with pytest.raises(ValueError, match="conflict with result columns"):
        sql_native._run_sql_native_task(
            "analytics",
            {},
            {"metric_name": "override"},
        )


def test_sql_string_literal_escapes_quotes() -> None:
    assert sql_native._sql_string_literal("O'Brien") == "'O''Brien'"
