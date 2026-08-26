from __future__ import annotations

from tests.ab_utils._support.edge_cases import (
    cuped_module,
    math,
    pd,
    pytest,
)


def test_cuped_p_value_wrapper_and_prepared_context_failures() -> None:
    frame = pd.DataFrame(
        {
            "user": [1, 2, 3, 4],
            "group": ["control", "control", "test", "test"],
            "metric": [1.0, 2.0, 2.0, 4.0],
        }
    )
    pre = pd.DataFrame({"user": [1, 2, 3, 4], "metric": [1.0, 2.0, 1.5, 3.0]})
    metric = {"kind": "mean", "metric_key": "metric", "column": "metric"}

    p_value = cuped_module._compute_cuped_p_value(
        frame,
        pre,
        "group",
        "user",
        "control",
        "test",
        metric,
    )
    assert 0 <= p_value <= 1

    comparison = frame[["user", "group"]]
    built, reason = cuped_module._build_cuped_frame_from_prepared_context(
        comparison,
        "user",
        {"exp_values": None, "exp_error": "missing experiment"},
    )
    assert built is None
    assert "missing experiment" in str(reason)


@pytest.mark.parametrize("missing_side", ["experiment", "pre"])
def test_cuped_frame_reports_missing_metric_values(missing_side: str) -> None:
    experiment = pd.DataFrame({"user": [1], "group": ["control"], "metric": [1.0]})
    pre = pd.DataFrame({"user": [1], "metric": [1.0]})
    if missing_side == "experiment":
        experiment = experiment.drop(columns="metric")
    else:
        pre = pre.drop(columns="metric")

    built, reason = cuped_module._build_cuped_frame(
        experiment,
        pre,
        "user",
        "group",
        "control",
        "test",
        {"kind": "mean", "metric_key": "metric", "column": "metric"},
    )
    assert built is None
    expected_side = "pre-experiment" if missing_side == "pre" else missing_side
    assert f"{expected_side} metric values are unavailable" in str(reason)


def test_cuped_frame_reports_no_overlap_and_missing_ratio_columns() -> None:
    experiment = pd.DataFrame({"user": [1], "group": ["control"], "metric": [1.0]})
    pre = pd.DataFrame({"user": [2], "metric": [2.0]})
    built, reason = cuped_module._build_cuped_frame(
        experiment,
        pre,
        "user",
        "group",
        "control",
        "test",
        {"kind": "mean", "metric_key": "metric", "column": "metric"},
    )
    assert built is None
    assert "no overlapping" in str(reason)

    values, error = cuped_module._build_metric_values_by_user(
        pd.DataFrame({"user": [1], "numerator": [1.0]}),
        "user",
        {
            "kind": "ratio",
            "metric_key": "ratio",
            "ratio_spec": {
                "numerator": "numerator",
                "denominator": "denominator",
                "level": "agg",
            },
        },
        "value",
    )
    assert values.empty
    assert "denominator" in str(error)


def test_cuped_aggregate_ratio_reports_invalid_denominator() -> None:
    values, error = cuped_module._build_metric_values_by_user(
        pd.DataFrame({"user": [1, 2], "numerator": [1.0, 2.0], "denominator": [0.0, 0.0]}),
        "user",
        {
            "kind": "ratio",
            "metric_key": "ratio",
            "ratio_spec": {
                "numerator": "numerator",
                "denominator": "denominator",
                "level": "agg",
            },
        },
        "value",
    )
    assert values.empty
    assert error is not None


def test_cuped_p_value_from_prepared_frame_wrapper() -> None:
    frame = pd.DataFrame(
        {
            "group": ["control", "control", "test", "test"],
            "metric_exp": [1.0, 2.0, 3.0, 5.0],
            "metric_pre": [1.0, 2.0, 2.0, 4.0],
        }
    )
    p_value, reason = cuped_module._compute_cuped_p_value_from_frame(
        frame, "group", "control", "test"
    )
    assert 0 <= p_value <= 1
    assert reason is None


def test_cuped_nan_standard_error_returns_unavailable_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = pd.DataFrame(
        {
            "group": ["control", "control", "test", "test"],
            "metric_exp": [1.0, 3.0, 4.0, 7.0],
            "metric_pre": [1.0, 2.0, 2.0, 4.0],
        }
    )
    monkeypatch.setattr(
        cuped_module,
        "_compute_group_diff_standard_error",
        lambda **_kwargs: math.nan,
    )

    p_value, standard_error, reason = cuped_module._compute_cuped_statistics_from_frame(
        frame,
        "group",
        "control",
        "test",
    )

    assert math.isnan(p_value)
    assert math.isnan(standard_error)
    assert reason == "not enough overlapping observations to run the CUPED t-test"
