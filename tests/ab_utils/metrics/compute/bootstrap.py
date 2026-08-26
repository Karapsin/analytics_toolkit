from __future__ import annotations

from tests.ab_utils._support.metrics import (
    _build_sample_metrics_df,
    _manual_centered_bootstrap_adjustment,
    bootstrap_module,
    compute_test_metrics,
    inspect,
    math,
    np,
    pd,
    pytest,
)


def test_compute_test_metrics_bootstrap_progress_defaults_to_false() -> None:
    signature = inspect.signature(compute_test_metrics)

    assert signature.parameters["bootstrap_progress"].default is False


@pytest.mark.parametrize(
    "outliers_policy",
    ["truncate", "drop", "non_zero_truncate"],
)
def test_compute_test_metrics_matches_centered_bootstrap_oracle(
    outliers_policy: str,
) -> None:
    df = pd.DataFrame(
        {
            "user_id": range(36),
            "group_name": ["control"] * 12 + ["test_a"] * 12 + ["test_b"] * 12,
            "orders": [
                0,
                0,
                4,
                5,
                6,
                7,
                8,
                9,
                10,
                11,
                12,
                30,
                3,
                4,
                5,
                6,
                7,
                8,
                9,
                10,
                11,
                12,
                13,
                35,
                1,
                3,
                5,
                7,
                9,
                11,
                13,
                15,
                17,
                19,
                21,
                40,
            ],
        }
    )

    result = compute_test_metrics(
        df,
        multiple_comparisons_adjustment=True,
        multiple_comparisons_adjustment_resamples=80,
        bootstrap_random_state=11,
        bootstrap_n_jobs=1,
        outliers_quantile=0.8,
        outliers_policy=outliers_policy,
    )
    expected = _manual_centered_bootstrap_adjustment(
        df,
        group="group_name",
        control="control",
        metric_kind="mean",
        metric_columns=("orders",),
        test_vs_test=True,
        resamples=80,
        random_state=11,
        outliers_quantile=0.8,
        outliers_policy=outliers_policy,
    )

    for row in result.to_dict("records"):
        expected_p, expected_se = expected[(row["group_1"], row["group_2"])]
        assert row["bootstrap_adj_p"] == pytest.approx(expected_p)
        assert row["s.e. bootstrap"] == pytest.approx(expected_se)


@pytest.mark.parametrize("ratio_level", ["user", "agg"])
def test_compute_test_metrics_ratio_bootstrap_matches_independent_oracle(
    ratio_level: str,
) -> None:
    denominator = np.tile(np.array([5.0, 8.0, 10.0, 12.0, 15.0, 20.0]), 8)
    numerator = np.concatenate(
        [
            denominator[:24] * np.linspace(0.1, 0.9, 24),
            denominator[24:] * np.linspace(0.2, 1.1, 24),
        ]
    )
    numerator[-1] = denominator[-1] * 8
    df = pd.DataFrame(
        {
            "user_id": range(48),
            "group_name": ["control"] * 24 + ["test"] * 24,
            "clicks": numerator,
            "impressions": denominator,
        }
    )
    ratio_metrics = [
        {
            "name": "ctr",
            "numerator": "clicks",
            "denominator": "impressions",
            "level": ratio_level,
        }
    ]

    result = compute_test_metrics(
        df,
        ratio_metrics=ratio_metrics,
        test_vs_test=False,
        multiple_comparisons_adjustment=True,
        multiple_comparisons_adjustment_resamples=60,
        bootstrap_random_state=23,
        outliers_quantile=0.85,
        outliers_policy="truncate",
    )
    expected = _manual_centered_bootstrap_adjustment(
        df,
        group="group_name",
        control="control",
        metric_kind=ratio_level,
        metric_columns=("clicks", "impressions"),
        test_vs_test=False,
        resamples=60,
        random_state=23,
        outliers_quantile=0.85,
        outliers_policy="truncate",
    )[("test", "control")]

    ratio_row = result[result["metric_name"] == "ctr"].iloc[0]
    assert ratio_row["bootstrap_adj_p"] == pytest.approx(expected[0])
    assert ratio_row["s.e. bootstrap"] == pytest.approx(expected[1])


def test_compute_test_metrics_centered_bootstrap_extreme_effect_reaches_finite_floor() -> None:
    control = np.linspace(-1.0, 1.0, 50)
    test = np.linspace(99.0, 101.0, 50)
    df = pd.DataFrame(
        {
            "user_id": range(100),
            "group_name": ["control"] * 50 + ["test"] * 50,
            "value": np.concatenate([control, test]),
        }
    )

    result = compute_test_metrics(
        df,
        test_vs_test=False,
        multiple_comparisons_adjustment=True,
        multiple_comparisons_adjustment_resamples=199,
        bootstrap_random_state=7,
        outliers_quantile=1,
    )

    row = result.iloc[0]
    assert row["p-value"] < 1e-50
    assert row["bootstrap_adj_p"] == pytest.approx(1 / 200)


def test_compute_test_metrics_centered_bootstrap_is_one_for_exact_null() -> None:
    values = np.linspace(-2.0, 2.0, 40)
    df = pd.DataFrame(
        {
            "user_id": range(80),
            "group_name": ["control"] * 40 + ["test"] * 40,
            "value": np.concatenate([values, values]),
        }
    )

    result = compute_test_metrics(
        df,
        test_vs_test=False,
        multiple_comparisons_adjustment=True,
        multiple_comparisons_adjustment_resamples=99,
        bootstrap_random_state=5,
        outliers_quantile=1,
    )

    assert result.iloc[0]["bootstrap_adj_p"] == 1


def test_compute_test_metrics_aggregate_ratio_bootstrap_is_scale_invariant() -> None:
    df = pd.DataFrame(
        {
            "user_id": range(60),
            "group_name": ["control"] * 30 + ["test"] * 30,
            "clicks": np.linspace(1.0, 30.0, 60),
            "impressions": np.linspace(10.0, 90.0, 60),
        }
    )
    ratio_metrics = [
        {
            "name": "ctr",
            "numerator": "clicks",
            "denominator": "impressions",
            "level": "agg",
        }
    ]
    kwargs = {
        "ratio_metrics": ratio_metrics,
        "test_vs_test": False,
        "multiple_comparisons_adjustment": True,
        "multiple_comparisons_adjustment_resamples": 75,
        "bootstrap_random_state": 29,
        "outliers_quantile": 1,
    }

    original = compute_test_metrics(df, **kwargs)
    scaled_df = df.copy()
    scaled_df[["clicks", "impressions"]] *= 100
    scaled = compute_test_metrics(scaled_df, **kwargs)
    original_ratio = original[original["metric_name"] == "ctr"].iloc[0]
    scaled_ratio = scaled[scaled["metric_name"] == "ctr"].iloc[0]

    assert scaled_ratio["bootstrap_adj_p"] == pytest.approx(original_ratio["bootstrap_adj_p"])
    assert scaled_ratio["s.e. bootstrap"] == pytest.approx(original_ratio["s.e. bootstrap"])


@pytest.mark.filterwarnings("ignore:Precision loss occurred in moment calculation:RuntimeWarning")
@pytest.mark.filterwarnings("ignore:Bootstrap discarded .* resamples:RuntimeWarning")
def test_compute_test_metrics_bootstrap_is_deterministic_across_executors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    df = _build_sample_metrics_df()

    serial = compute_test_metrics(
        df,
        multiple_comparisons_adjustment=True,
        multiple_comparisons_adjustment_resamples=30,
        bootstrap_random_state=17,
        bootstrap_n_jobs=1,
    )
    process = compute_test_metrics(
        df,
        multiple_comparisons_adjustment=True,
        multiple_comparisons_adjustment_resamples=30,
        bootstrap_random_state=17,
        bootstrap_n_jobs=2,
    )

    def unavailable_process_pool(*args: object, **kwargs: object) -> None:
        raise PermissionError("process pools unavailable")

    monkeypatch.setattr(bootstrap_module, "ProcessPoolExecutor", unavailable_process_pool)
    thread_fallback = compute_test_metrics(
        df,
        multiple_comparisons_adjustment=True,
        multiple_comparisons_adjustment_resamples=30,
        bootstrap_random_state=17,
        bootstrap_n_jobs=2,
    )

    pd.testing.assert_frame_equal(serial, process)
    pd.testing.assert_frame_equal(serial, thread_fallback)
    assert serial.columns[serial.columns.get_loc("p-value") + 1] == "s.e. bootstrap"
    assert serial.columns[serial.columns.get_loc("s.e. bootstrap") + 1] == "bootstrap_adj_p"
    orders_row = serial[
        (serial["group_1"] == "test_a")
        & (serial["group_2"] == "control")
        & (serial["metric_name"] == "orders")
    ].iloc[0]
    assert not math.isnan(float(orders_row["s.e. bootstrap"]))


def test_compute_test_metrics_accepts_bootstrap_progress() -> None:
    df = _build_sample_metrics_df()

    result = compute_test_metrics(
        df,
        multiple_comparisons_adjustment=True,
        multiple_comparisons_adjustment_resamples=5,
        bootstrap_random_state=0,
        bootstrap_progress=True,
    )

    assert "s.e. bootstrap" in result.columns
    assert "bootstrap_adj_p" in result.columns


@pytest.mark.parametrize(
    ("kwargs", "error_type", "message"),
    [
        (
            {"bootstrap_random_state": True},
            TypeError,
            "bootstrap_random_state must be an integer or None",
        ),
        (
            {"bootstrap_random_state": -1},
            ValueError,
            "bootstrap_random_state must be non-negative or None",
        ),
        ({"bootstrap_n_jobs": 0}, ValueError, "bootstrap_n_jobs must be positive"),
        ({"bootstrap_n_jobs": True}, TypeError, "bootstrap_n_jobs must be an integer"),
        ({"bootstrap_progress": 1}, TypeError, "bootstrap_progress must be a boolean"),
    ],
)
def test_compute_test_metrics_validates_bootstrap_parameters(
    kwargs: dict[str, object],
    error_type: type[Exception],
    message: str,
) -> None:
    df = _build_sample_metrics_df()

    with pytest.raises(error_type, match=message):
        compute_test_metrics(df, **kwargs)
