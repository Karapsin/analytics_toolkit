from __future__ import annotations

from tests.ab_utils._support.metrics import (
    Any,
    RatioMetricSpec,
    Sequence,
    SimpleNamespace,
    _single_metric_row,
    ab_metrics,
    ab_utils,
    compute_mde,
    compute_mde_from_sql,
    compute_mde_sql_native,
    math,
    pd,
    planning_module,
    pytest,
)


def test_compute_mde_computes_agg_ratio_delta_method_unit_variance() -> None:
    df = pd.DataFrame(
        {
            "user_id": [1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3],
            "dt": pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"] * 3),
            "clicks": [
                1.0,
                1.0,
                1.0,
                1.0,
                1.0,
                1.0,
                2.0,
                2.0,
                5.0,
                5.0,
                3.0,
                3.0,
            ],
            "impressions": [
                10.0,
                10.0,
                5.0,
                5.0,
                10.0,
                10.0,
                5.0,
                5.0,
                10.0,
                10.0,
                15.0,
                15.0,
            ],
        }
    )

    result = compute_mde(
        df,
        user_id="user_id",
        metric_columns=[],
        ratio_metrics=[
            RatioMetricSpec(
                name="ctr_agg",
                numerator="clicks",
                denominator="impressions",
                level="agg",
            )
        ],
        group_sizes=[10],
        exp_days=[2],
        start_dt="2024-01-03",
        outliers_quantile=1,
    )

    row = _single_metric_row(result, "ctr_agg")
    numerator = pd.Series([2.0, 4.0, 6.0])
    denominator = pd.Series([10.0, 10.0, 30.0])
    ratio = float(numerator.sum() / denominator.sum())
    centered = numerator - ratio * denominator
    expected_variance = float(centered.var(ddof=1)) / float(denominator.mean()) ** 2
    assert row["avg"] == pytest.approx(ratio)
    assert row["var"] == pytest.approx(expected_variance)
    assert not math.isnan(float(row["mde_abs_cuped"]))
    assert not math.isnan(float(row["mde_relative_cuped"]))


def test_compute_mde_applies_aggregation_policy_to_agg_ratio_components() -> None:
    df = pd.DataFrame(
        {
            "user_id": [1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3],
            "dt": pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"] * 3),
            "converted": [0.0, 0.0, 0.0, 1.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0],
            "visits": [10.0] * 12,
        }
    )

    result = compute_mde(
        df,
        user_id="user_id",
        metric_columns=[],
        ratio_metrics=[
            RatioMetricSpec(
                name="conversion_rate",
                numerator="converted",
                denominator="visits",
                level="agg",
            )
        ],
        group_sizes=[10],
        exp_days=[2],
        start_dt="2024-01-03",
        max_agg_metrics=["converted"],
        outliers_quantile=1,
    )

    row = _single_metric_row(result, "conversion_rate")
    numerator = pd.Series([1.0, 0.0, 1.0])
    denominator = pd.Series([20.0, 20.0, 20.0])
    ratio = float(numerator.sum() / denominator.sum())
    centered = numerator - ratio * denominator
    expected_variance = float(centered.var(ddof=1)) / float(denominator.mean()) ** 2
    assert row["avg"] == pytest.approx(ratio)
    assert row["var"] == pytest.approx(expected_variance)


def test_compute_mde_defaults_to_first_historical_date_and_accepts_start_dt() -> None:
    df = pd.DataFrame(
        {
            "user_id": [1, 2] * 6,
            "dt": pd.to_datetime(
                [
                    "2024-01-01",
                    "2024-01-01",
                    "2024-01-02",
                    "2024-01-02",
                    "2024-01-03",
                    "2024-01-03",
                    "2024-01-04",
                    "2024-01-04",
                    "2024-01-05",
                    "2024-01-05",
                    "2024-01-06",
                    "2024-01-06",
                ]
            ),
            "orders": [
                1.0,
                3.0,
                1.0,
                3.0,
                10.0,
                20.0,
                10.0,
                20.0,
                100.0,
                300.0,
                100.0,
                300.0,
            ],
        }
    )

    with pytest.warns(UserWarning, match="Could not compute CUPED MDE"):
        default_result = compute_mde(
            df,
            user_id="user_id",
            metric_columns=["orders"],
            group_sizes=[10],
            exp_days=[2],
            start_dt=None,
            outliers_quantile=1,
        )
    explicit_result = compute_mde(
        df,
        user_id="user_id",
        metric_columns=["orders"],
        group_sizes=[10],
        exp_days=[2],
        start_dt="2024-01-05",
        outliers_quantile=1,
    )

    assert _single_metric_row(default_result, "orders")["avg"] == pytest.approx(4.0)
    assert _single_metric_row(explicit_result, "orders")["avg"] == pytest.approx(400.0)


def test_compute_mde_defaults_user_id_argument() -> None:
    df = pd.DataFrame(
        {
            "user_id": [1, 2, 1, 2],
            "dt": ["2024-01-01", "2024-01-01", "2024-01-02", "2024-01-02"],
            "orders": [10.0, 12.0, 14.0, 18.0],
        }
    )

    result = compute_mde(
        df,
        metric_columns=["orders"],
        group_sizes=[10],
        exp_days=[1],
        start_dt="2024-01-02",
        outliers_quantile=1,
    )

    row = _single_metric_row(result, "orders")
    assert row["avg"] == pytest.approx(16.0)
    assert row["var"] == pytest.approx(8.0)


def test_compute_mde_variants_match_for_same_dataset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_df = pd.DataFrame(
        {
            "user_id": [user_id for user_id in range(1, 6) for _ in range(4)],
            "dt": pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"] * 5),
            "orders": [
                1.0,
                2.0,
                4.0,
                6.0,
                2.0,
                3.0,
                5.0,
                7.0,
                4.0,
                5.0,
                7.0,
                10.0,
                3.0,
                6.0,
                8.0,
                12.0,
                5.0,
                8.0,
                13.0,
                17.0,
            ],
            "clicks": [
                2.0,
                3.0,
                4.0,
                5.0,
                1.0,
                4.0,
                5.0,
                7.0,
                3.0,
                5.0,
                6.0,
                9.0,
                4.0,
                6.0,
                8.0,
                10.0,
                5.0,
                7.0,
                9.0,
                12.0,
            ],
            "views": [
                10.0,
                12.0,
                14.0,
                16.0,
                8.0,
                10.0,
                15.0,
                18.0,
                11.0,
                13.0,
                17.0,
                20.0,
                12.0,
                15.0,
                19.0,
                23.0,
                13.0,
                16.0,
                21.0,
                26.0,
            ],
            "converted": [
                0.0,
                1.0,
                0.0,
                1.0,
                0.0,
                0.0,
                1.0,
                1.0,
                1.0,
                1.0,
                1.0,
                1.0,
                0.0,
                1.0,
                1.0,
                1.0,
                1.0,
                1.0,
                1.0,
                1.0,
            ],
        }
    )
    ratio_metrics = [
        RatioMetricSpec(
            name="ctr_user",
            numerator="clicks",
            denominator="views",
            level="user",
        ),
        RatioMetricSpec(
            name="ctr_agg",
            numerator="clicks",
            denominator="views",
            level="agg",
        ),
    ]
    common_kwargs = {
        "user_id": "user_id",
        "metric_columns": ["orders", "converted"],
        "ratio_metrics": ratio_metrics,
        "group_sizes": [10, 14],
        "exp_days": [1, 2],
        "start_dt": "2024-01-03",
        "control_share": 0.6,
        "outliers_quantile": 1,
        "max_agg_metrics": ["converted"],
    }
    expected = compute_mde(source_df, **common_kwargs)
    table_info = SimpleNamespace(
        exists=True,
        columns={
            "user_id": "int",
            "dt": "date",
            "orders": "double precision",
            "clicks": "double precision",
            "views": "double precision",
            "converted": "double precision",
        },
        backend="gp",
        table="sandbox.events",
        resolved_table=None,
    )

    def aggregate_window(start: str, days: int) -> pd.DataFrame:
        start_date = pd.Timestamp(start)
        mask = (source_df["dt"] >= start_date) & (
            source_df["dt"] < start_date + pd.Timedelta(days=days)
        )
        return (
            source_df.loc[mask]
            .groupby("user_id", as_index=False)
            .agg(
                {
                    "orders": "sum",
                    "clicks": "sum",
                    "views": "sum",
                    "converted": "max",
                }
            )
        )

    def fake_table_info(db_key: str, table: str) -> SimpleNamespace:
        assert db_key == "analytics"
        assert table == "sandbox.events"
        return table_info

    def fake_read(db_key: str, query: str, **kwargs: object) -> pd.DataFrame:
        assert db_key == "analytics"
        assert kwargs["query_label"] in {"mde-parity", "mde-native-parity"}
        if "COUNT(*) AS row_count" in query:
            return pd.DataFrame(
                {
                    "row_count": [len(source_df)],
                    "null_user_rows": [0],
                    "null_date_rows": [0],
                    "min_dt": [pd.Timestamp("2024-01-01")],
                    "max_dt": [pd.Timestamp("2024-01-04")],
                }
            )
        if "duplicate_user_day_rows" in query:
            return pd.DataFrame({"duplicate_user_day_rows": [0]})
        raise AssertionError(f"Unexpected direct aggregate query:\n{query}")

    def fake_parallel_sql(tasks: object, **kwargs: object) -> dict[str, pd.DataFrame]:
        assert kwargs["concurrency"] == 1
        frames: dict[str, pd.DataFrame] = {}
        for task in tasks:
            task_name = str(task["name"])
            assert task["db_key"] == "analytics"
            assert task["query_label"] == "mde-parity"
            if task_name == "mde_outcome_1":
                frames[task_name] = aggregate_window("2024-01-03", 1)
            elif task_name == "mde_outcome_2":
                frames[task_name] = aggregate_window("2024-01-03", 2)
            elif task_name == "mde_pre_1":
                frames[task_name] = aggregate_window("2024-01-02", 1)
            elif task_name == "mde_pre_2":
                frames[task_name] = aggregate_window("2024-01-01", 2)
            else:
                raise AssertionError(f"Unexpected SQL task name: {task_name}")
        return frames

    def fake_load_sql_native_mde_stats(
        *,
        metric_definitions: Sequence[dict[str, object]],
        aggregation_policies: dict[str, str],
        days_values: Sequence[int],
        windows: dict[int, dict[str, Any]],
        outliers_quantile: float,
        outliers_policy: str,
        **kwargs: object,
    ) -> dict[tuple[int, int], dict[str, object]]:
        del kwargs
        stats_by_metric_day: dict[tuple[int, int], dict[str, object]] = {}
        for metric_index, metric_definition in enumerate(metric_definitions):
            for days in days_values:
                window = windows[int(days)]
                window_df = planning_module._filter_mde_window(
                    df=source_df,
                    date_column="dt",
                    start_date=window["outcome_start"],
                    days=int(days),
                )
                user_metric_df = planning_module._aggregate_mde_window_to_users(
                    df=window_df,
                    metric_definition=metric_definition,
                    user_id="user_id",
                    aggregation_policies=aggregation_policies,
                )
                outlier_context = planning_module._build_outlier_context(
                    df=user_metric_df,
                    metric_definition=metric_definition,
                    outliers_quantile=outliers_quantile,
                    outliers_policy=outliers_policy,
                )
                metric_stats = planning_module._compute_mde_metric_stats(
                    df=user_metric_df,
                    metric_definition=metric_definition,
                    outlier_context=outlier_context,
                )
                cuped_variance, cuped_reason = planning_module._compute_mde_cuped_variance(
                    df=source_df,
                    date_column="dt",
                    user_id="user_id",
                    metric_definition=metric_definition,
                    outcome_user_metric_df=user_metric_df,
                    outcome_outlier_context=outlier_context,
                    pre_start_date=window["pre_start"],
                    pre_days=window["pre_days"],
                    unavailable_reason=window["cuped_unavailable_reason"],
                    outliers_quantile=outliers_quantile,
                    outliers_policy=outliers_policy,
                    aggregation_policies=aggregation_policies,
                )
                assert cuped_reason is None
                stats_by_metric_day[(metric_index, int(days))] = {
                    "avg": metric_stats["avg"],
                    "var": metric_stats["var"],
                    "cuped_pair_n": 5,
                    "cuped_pre_var": 1.0,
                    "cuped_adjusted_var": cuped_variance,
                }
        return stats_by_metric_day

    monkeypatch.setattr(
        "analytics_toolkit.ab_utils.planning.sql_facade.table_info",
        fake_table_info,
    )
    monkeypatch.setattr(
        "analytics_toolkit.ab_utils.planning.sql_facade.read",
        fake_read,
    )
    monkeypatch.setattr(
        "analytics_toolkit.ab_utils.planning.sql_facade.parallel_sql",
        fake_parallel_sql,
    )

    sql_result = compute_mde_from_sql(
        "analytics",
        "sandbox.events",
        **common_kwargs,
        query_label="mde-parity",
    )
    monkeypatch.setattr(
        planning_module,
        "_load_sql_native_mde_stats",
        fake_load_sql_native_mde_stats,
    )
    native_result = compute_mde_sql_native(
        "analytics",
        "sandbox.events",
        **common_kwargs,
        query_label="mde-native-parity",
    )

    pd.testing.assert_frame_equal(sql_result, expected)
    pd.testing.assert_frame_equal(native_result, expected)


def test_mde_planning_options_is_no_longer_exported() -> None:
    assert not hasattr(ab_utils, "MdePlanningOptions")
    assert not hasattr(ab_metrics, "MdePlanningOptions")
    assert not hasattr(ab_utils, "compute_mde_only")
    assert not hasattr(ab_metrics, "compute_mde_only")
    assert hasattr(ab_utils, "compute_mde")
    assert hasattr(ab_metrics, "compute_mde")


def test_mde_public_entrypoints_require_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    frame = pd.DataFrame({"user": [1], "dt": ["2026-01-01"]})
    with pytest.raises(ValueError, match="At least one metric"):
        compute_mde(
            frame,
            user_id="user",
            metric_columns=[],
            group_sizes=[2],
            exp_days=[1],
            start_dt=None,
        )

    table_info = SimpleNamespace(
        exists=True,
        columns=["user", "dt"],
        resolved_table="public.events",
        table="events",
        backend="gp",
    )
    monkeypatch.setattr(planning_module.sql_facade, "table_info", lambda *_args: table_info)
    for entrypoint in (compute_mde_from_sql, compute_mde_sql_native):
        with pytest.raises(ValueError, match="At least one metric"):
            entrypoint(
                "db",
                "public.events",
                user_id="user",
                metric_columns=[],
                group_sizes=[2],
                exp_days=[1],
                start_dt=None,
            )


def test_parallel_mde_preserves_first_exception_and_cancels_all_futures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_failure = RuntimeError("first failure")

    class FakeFuture:
        def __init__(self) -> None:
            self.cancelled = False

        def result(self) -> object:
            raise first_failure

        def cancel(self) -> None:
            self.cancelled = True

    futures = [FakeFuture(), FakeFuture()]
    exited: list[type[BaseException] | None] = []

    class FakeExecutor:
        def __init__(self, *, max_workers: int) -> None:
            assert max_workers == 2
            self.index = 0

        def __enter__(self) -> Any:
            return self

        def __exit__(self, exc_type: type[BaseException] | None, *_args: object) -> None:
            exited.append(exc_type)

        def submit(self, *_args: object, **_kwargs: object) -> FakeFuture:
            future = futures[self.index]
            self.index += 1
            return future

    def in_completion_order(mapping: dict[FakeFuture, object]) -> Any:
        return iter(mapping)

    monkeypatch.setattr(planning_module, "ThreadPoolExecutor", FakeExecutor)
    monkeypatch.setattr(planning_module, "as_completed", in_completion_order)

    with pytest.raises(RuntimeError, match="first failure"):
        planning_module._compute_parallel_sql_mde_rows(
            concurrency=2,
            user_id="user",
            metric_definitions=[{"kind": "mean", "metric_key": "metric", "column": "metric"}],
            days_values=[1],
            planned_splits=[
                {"group_size": 10, "control_n": 5, "test_n": 5},
                {"group_size": 20, "control_n": 10, "test_n": 10},
            ],
            control_share=0.5,
            windows={1: {"pre_days": 1}},
            outcome_frames={1: pd.DataFrame()},
            pre_frames={1: None},
            outliers_quantile=0.99,
            outliers_policy="truncate",
            mde_alpha=0.05,
            mde_power=0.8,
        )

    assert all(future.cancelled for future in futures)
    assert exited == [RuntimeError]


def test_mde_planning_row_keeps_nan_variance_outputs_nan() -> None:
    row = planning_module._build_mde_planning_row(
        metric_name="metric",
        avg=2.0,
        variance=math.nan,
        days=1,
        pre_exp_days=1,
        group_size=10,
        control_share=0.5,
        control_n=5,
        test_n=5,
        cuped_variance=math.nan,
        mde_alpha=0.05,
        mde_power=0.8,
    )
    assert math.isnan(float(row["mde_abs"]))
    assert math.isnan(float(row["mde_abs_cuped"]))
