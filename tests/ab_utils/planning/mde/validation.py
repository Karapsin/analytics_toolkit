from __future__ import annotations

from tests.ab_utils._support.metrics import (
    RatioMetricSpec,
    compute_mde,
    compute_mde_from_sql,
    compute_mde_sql_native,
    inspect,
    math,
    pd,
    planning_module,
    pytest,
)


def test_compute_mde_start_dt_is_required() -> None:
    assert inspect.signature(compute_mde).parameters["start_dt"].default is inspect._empty
    assert inspect.signature(compute_mde_from_sql).parameters["start_dt"].default is inspect._empty
    assert (
        inspect.signature(compute_mde_sql_native).parameters["start_dt"].default is inspect._empty
    )


@pytest.mark.parametrize("value", [True, [], pd.NaT])
def test_mde_start_date_normalization_rejects_invalid_values(value: object) -> None:
    error = TypeError if value is True else ValueError
    with pytest.raises(error, match="start_dt must be a datelike value"):
        planning_module._normalize_start_dt(value)


def test_mde_metric_column_normalization_rejects_duplicates_and_missing() -> None:
    frame = pd.DataFrame({"user": [1], "dt": ["2026-01-01"], "metric": [1.0]})
    kwargs = {
        "df": frame,
        "ratio_specs": [],
        "user_id": "user",
        "date_column": "dt",
    }
    with pytest.raises(ValueError, match="must not contain duplicates"):
        planning_module._normalize_metric_columns(metric_columns=["metric", "metric"], **kwargs)
    with pytest.raises(ValueError, match="Missing metric"):
        planning_module._normalize_metric_columns(metric_columns=["missing"], **kwargs)


def test_mde_positive_grid_rejects_conflicting_empty_and_incomplete_inputs() -> None:
    names = {
        "values_name": "values",
        "min_name": "minimum",
        "max_name": "maximum",
        "step_name": "step",
    }
    with pytest.raises(ValueError, match="cannot be combined"):
        planning_module._resolve_positive_int_grid(
            values=[1], min_value=1, max_value=None, step=None, **names
        )
    with pytest.raises(ValueError, match="must not be empty"):
        planning_module._resolve_positive_int_grid(
            values=[], min_value=None, max_value=None, step=None, **names
        )
    with pytest.raises(ValueError, match="Either values"):
        planning_module._resolve_positive_int_grid(
            values=None, min_value=1, max_value=None, step=1, **names
        )
    with pytest.raises(ValueError, match="less than or equal"):
        planning_module._resolve_positive_int_grid(
            values=None, min_value=3, max_value=1, step=1, **names
        )


@pytest.mark.parametrize(
    ("value", "error"),
    [(True, TypeError), (1.5, TypeError), (0, ValueError), (-1, ValueError)],
)
def test_mde_positive_integer_validation(value: object, error: type[Exception]) -> None:
    with pytest.raises(error, match="positive integers"):
        planning_module._validate_positive_int(value, "value")


@pytest.mark.parametrize(
    ("value", "error"),
    [
        (True, TypeError),
        ("0.5", TypeError),
        (math.inf, ValueError),
        (0, ValueError),
        (1, ValueError),
    ],
)
def test_mde_control_share_validation(value: object, error: type[Exception]) -> None:
    with pytest.raises(error, match="control_share"):
        planning_module._validate_control_share(value)


def test_mde_split_rejects_empty_arm() -> None:
    with pytest.raises(ValueError, match="one control and one test"):
        planning_module._build_planned_split(group_size=1, control_share=0.5)


@pytest.mark.parametrize(
    ("frame", "message"),
    [
        (pd.DataFrame(), "at least one user-day"),
        (pd.DataFrame({"dt": ["2026-01-01"]}), "Column 'user' was not found"),
        (pd.DataFrame({"user": [1]}), "Column 'dt' was not found"),
        (
            pd.DataFrame({"user": [None], "dt": ["2026-01-01"]}),
            "user.*missing values",
        ),
        (pd.DataFrame({"user": [1], "dt": [None]}), "dt.*missing values"),
        (pd.DataFrame({"user": [1], "dt": ["not-a-date"]}), "datelike values"),
        (
            pd.DataFrame({"user": [1, 1], "dt": ["2026-01-01", "2026-01-01"]}),
            "unique user-day rows",
        ),
    ],
)
def test_prepare_mde_user_day_frame_rejects_invalid_rows(frame: pd.DataFrame, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        planning_module._prepare_mde_user_day_frame(df=frame, user_id="user", date_column="dt")


def test_compute_mde_rejects_start_dt_outside_history() -> None:
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
                2.0,
                3.0,
                4.0,
                5.0,
                6.0,
                7.0,
                8.0,
                9.0,
                10.0,
                11.0,
                12.0,
            ],
        }
    )

    with pytest.raises(ValueError, match="before the first available historical date"):
        compute_mde(
            df,
            user_id="user_id",
            metric_columns=["orders"],
            group_sizes=[10],
            exp_days=[2],
            start_dt="2023-12-31",
            outliers_quantile=1,
        )
    with pytest.raises(ValueError, match="exceeds the available historical span"):
        compute_mde(
            df,
            user_id="user_id",
            metric_columns=["orders"],
            group_sizes=[10],
            exp_days=[2],
            start_dt="2024-01-06",
            outliers_quantile=1,
        )


def test_compute_mde_rejects_invalid_grid_inputs() -> None:
    df = pd.DataFrame(
        {"user_id": [1, 2], "dt": ["2024-01-01", "2024-01-01"], "orders": [10.0, 12.0]}
    )

    with pytest.raises(ValueError, match="group_sizes cannot be combined"):
        compute_mde(
            df,
            user_id="user_id",
            group_sizes=[10],
            min_group_size=10,
            exp_days=[1],
            start_dt=None,
        )
    with pytest.raises(ValueError, match="Either group_sizes"):
        compute_mde(df, user_id="user_id", exp_days=[1], start_dt=None)
    with pytest.raises(ValueError, match="exp_days cannot be combined"):
        compute_mde(
            df,
            user_id="user_id",
            group_sizes=[10],
            exp_days=[1],
            min_days=1,
            start_dt=None,
        )
    with pytest.raises(ValueError, match="control_share"):
        compute_mde(
            df,
            user_id="user_id",
            group_sizes=[10],
            exp_days=[1],
            start_dt=None,
            control_share=1.0,
        )
    with pytest.raises(ValueError, match="at least one control and one test user"):
        compute_mde(df, user_id="user_id", group_sizes=[1], exp_days=[1], start_dt=None)
    with pytest.raises(ValueError, match="pre_exp_days"):
        compute_mde(
            df,
            user_id="user_id",
            group_sizes=[10],
            exp_days=[1],
            start_dt=None,
            pre_exp_days=0,
        )
    with pytest.raises(TypeError, match="pre_exp_days"):
        compute_mde(
            df,
            user_id="user_id",
            group_sizes=[10],
            exp_days=[1],
            start_dt=None,
            pre_exp_days=True,
        )


def test_compute_mde_rejects_invalid_aggregation_policy_inputs() -> None:
    df = pd.DataFrame(
        {
            "user_id": [1, 2],
            "dt": ["2024-01-01", "2024-01-01"],
            "orders": [10.0, 12.0],
            "converted": [1.0, 0.0],
        }
    )

    with pytest.raises(ValueError, match="Only one of sum_agg_metrics or max_agg_metrics"):
        compute_mde(
            df,
            user_id="user_id",
            metric_columns=["orders", "converted"],
            group_sizes=[10],
            exp_days=[1],
            start_dt=None,
            sum_agg_metrics=["orders"],
            max_agg_metrics=["converted"],
        )
    with pytest.raises(ValueError, match="max_agg_metrics must not contain duplicates"):
        compute_mde(
            df,
            user_id="user_id",
            metric_columns=["orders", "converted"],
            group_sizes=[10],
            exp_days=[1],
            start_dt=None,
            max_agg_metrics=["converted", "converted"],
        )
    with pytest.raises(ValueError, match="unknown metric column"):
        compute_mde(
            df,
            user_id="user_id",
            metric_columns=["orders"],
            group_sizes=[10],
            exp_days=[1],
            start_dt=None,
            max_agg_metrics=["converted"],
        )


def test_compute_mde_rejects_invalid_user_day_grain() -> None:
    missing_user = pd.DataFrame(
        {"user_id": [1, None], "dt": ["2024-01-01", "2024-01-01"], "orders": [1.0, 2.0]}
    )
    missing_date = pd.DataFrame(
        {"user_id": [1, 2], "dt": ["2024-01-01", None], "orders": [1.0, 2.0]}
    )
    invalid_date = pd.DataFrame(
        {"user_id": [1, 2], "dt": ["2024-01-01", "not-a-date"], "orders": [1.0, 2.0]}
    )
    duplicate_user_day = pd.DataFrame(
        {
            "user_id": [1, 1],
            "dt": ["2024-01-01 01:00:00", "2024-01-01 23:00:00"],
            "orders": [1.0, 2.0],
        }
    )

    with pytest.raises(ValueError, match="must not contain missing values"):
        compute_mde(
            missing_user,
            user_id="user_id",
            group_sizes=[10],
            exp_days=[1],
            start_dt=None,
        )
    with pytest.raises(ValueError, match="must not contain missing values"):
        compute_mde(
            missing_date,
            user_id="user_id",
            group_sizes=[10],
            exp_days=[1],
            start_dt=None,
        )
    with pytest.raises(ValueError, match="datelike"):
        compute_mde(
            invalid_date,
            user_id="user_id",
            group_sizes=[10],
            exp_days=[1],
            start_dt=None,
        )
    with pytest.raises(ValueError, match="unique user-day rows"):
        compute_mde(
            duplicate_user_day,
            user_id="user_id",
            group_sizes=[10],
            exp_days=[1],
            start_dt=None,
        )


def test_compute_mde_rejects_ratio_name_conflicting_with_mean_metric() -> None:
    df = pd.DataFrame(
        {
            "user_id": [1, 2, 3],
            "dt": ["2024-01-01", "2024-01-01", "2024-01-01"],
            "ctr": [0.1, 0.2, 0.3],
            "clicks": [1.0, 2.0, 3.0],
            "impressions": [10.0, 10.0, 10.0],
        }
    )

    with pytest.raises(ValueError, match="conflicts with a mean metric column"):
        compute_mde(
            df,
            user_id="user_id",
            group_sizes=[10],
            exp_days=[1],
            start_dt=None,
            ratio_metrics=[
                RatioMetricSpec(
                    name="ctr",
                    numerator="clicks",
                    denominator="impressions",
                    level="user",
                )
            ],
        )


def test_prepare_mde_frame_rejects_nat_after_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pd, "to_datetime", lambda *_args, **_kwargs: pd.Series([pd.NaT]))
    with pytest.raises(ValueError, match="datelike values"):
        planning_module._prepare_mde_user_day_frame(
            df=pd.DataFrame({"user": [1], "dt": ["2026-01-01"]}),
            user_id="user",
            date_column="dt",
        )


def test_user_aggregation_rejects_unknown_policy_before_metric_exclusion() -> None:
    with pytest.raises(AssertionError, match="Unexpected MDE aggregation policy"):
        planning_module._aggregate_mde_columns_to_users(
            aggregate_frame=pd.DataFrame({"user": [1], "metric": [math.nan]}),
            user_id="user",
            columns=["metric"],
            aggregation_policies={"metric": "median"},
        )
