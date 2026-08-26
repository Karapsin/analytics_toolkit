from __future__ import annotations

from tests.ab_utils._support.edge_cases import (
    api_module,
    math,
    np,
    pd,
    pytest,
    rows_module,
    stats_module,
    validation_module,
)


def test_validate_input_columns_lists_every_missing_column() -> None:
    with pytest.raises(ValueError, match=r"'group_name'.*'user_id'"):
        validation_module._validate_input_columns(
            pd.DataFrame({"metric": [1.0]}),
            group="group_name",
            user_id="user_id",
        )


def test_numeric_metric_validation_and_safe_relative_edges() -> None:
    with pytest.raises(TypeError, match="contains non-numeric"):
        stats_module._get_numeric_metric_series(pd.DataFrame({"metric": [1, "bad"]}), "metric")
    assert math.isnan(stats_module._safe_relative(math.nan, 1.0))
    assert math.isnan(stats_module._safe_relative(1.0, math.nan))
    assert math.isnan(stats_module._safe_relative(1.0, 0.0))
    assert stats_module._safe_relative(4.0, 2.0) == 2.0


def test_changed_metric_defaults_preserves_every_explicit_override() -> None:
    pre_exp = pd.DataFrame({"id": [1]})
    ratio_metrics = [{"name": "ratio", "numerator": "a", "denominator": "b"}]

    assert api_module._changed_metric_defaults(
        group="arm",
        control="baseline",
        user_id="id",
        mde_alpha=0.01,
        mde_power=0.9,
        ratio_metrics=ratio_metrics,
        test_vs_test=False,
        multiple_comparisons_adjustment=True,
        multiple_comparisons_adjustment_resamples=99,
        bootstrap_random_state=None,
        bootstrap_n_jobs=2,
        bootstrap_progress=True,
        pre_exp_metrics_df=pre_exp,
        outliers_quantile=0.95,
        outliers_policy="truncate",
    ) == {
        "group": "arm",
        "control": "baseline",
        "user_id": "id",
        "mde_alpha": 0.01,
        "mde_power": 0.9,
        "ratio_metrics": ratio_metrics,
        "test_vs_test": False,
        "multiple_comparisons_adjustment": True,
        "multiple_comparisons_adjustment_resamples": 99,
        "bootstrap_random_state": None,
        "bootstrap_n_jobs": 2,
        "bootstrap_progress": True,
        "pre_exp_metrics_df": pre_exp,
        "outliers_quantile": 0.95,
        "outliers_policy": "truncate",
    }


@pytest.mark.parametrize("case", ["null_user", "duplicate_user", "null_group"])
def test_compute_test_metrics_rejects_invalid_identity_columns(case: str) -> None:
    frame = pd.DataFrame(
        {"user_id": [1, 2], "group_name": ["control", "test"], "metric": [1.0, 2.0]}
    )
    if case == "null_user":
        frame.loc[1, "user_id"] = np.nan
    elif case == "duplicate_user":
        frame["user_id"] = [1, 1]
    else:
        frame.loc[1, "group_name"] = None

    with pytest.raises(ValueError, match="must"):
        api_module._compute_test_metrics_dataframe(frame)


def test_compute_test_metrics_rejects_missing_metric_and_control() -> None:
    with pytest.raises(ValueError, match="at least one metric"):
        api_module._compute_test_metrics_dataframe(
            pd.DataFrame({"user_id": [1, 2], "group_name": ["control", "test"]})
        )
    with pytest.raises(ValueError, match="Control label"):
        api_module._compute_test_metrics_dataframe(
            pd.DataFrame({"user_id": [1, 2], "group_name": ["a", "b"], "metric": [1.0, 2.0]})
        )

    with pytest.raises(ValueError, match="missing column 'numerator'"):
        api_module._compute_test_metrics_dataframe(
            pd.DataFrame({"user_id": [1, 2], "group_name": ["control", "test"]}),
            ratio_metrics=[
                {
                    "name": "ratio",
                    "numerator": "numerator",
                    "denominator": "denominator",
                }
            ],
        )


def test_metric_row_residual_paths_cover_implicit_context_masks_and_ratio_variance() -> None:
    with pytest.raises(ValueError, match="At least one non-control group"):
        rows_module._build_comparisons(["control"], "control")

    mean_frame = pd.DataFrame(
        {
            "group": ["control", "control", "test", "test"],
            "metric": [1.0, 9.0, 2.0, 12.0],
        }
    )
    mean_definition = {
        "kind": "mean",
        "metric_key": "metric",
        "column": "metric",
        "_outlier_context": {"cutoff": 5.0, "policy": "truncate"},
    }
    prepared_mean = rows_module._prepare_metric_context(mean_frame, mean_definition)
    legacy_mean_row = rows_module._build_metric_row(
        mean_frame,
        "group",
        "control",
        "test",
        mean_definition,
        0.05,
        0.8,
    )
    prepared_mean_row = rows_module._build_metric_row(
        mean_frame,
        "group",
        "control",
        "test",
        mean_definition,
        0.05,
        0.8,
        prepared_metric_context=prepared_mean,
    )
    assert legacy_mean_row["outliers_n_group_2"] == 1
    assert prepared_mean_row["outliers_n_group_1"] == 1

    ratio_definition = {
        "kind": "ratio",
        "metric_key": "ratio",
        "ratio_spec": {
            "name": "ratio",
            "numerator": "num",
            "denominator": "den",
            "level": "agg",
        },
    }
    invalid_frame = pd.DataFrame(
        {
            "group": ["control", "control", "test", "test"],
            "num": [1.0, 2.0, 3.0, 4.0],
            "den": [0.0, 0.0, 0.0, 0.0],
        }
    )
    prepared_invalid = rows_module._prepare_metric_context(invalid_frame, ratio_definition)
    prepared_invalid_row = rows_module._build_metric_row(
        invalid_frame,
        "group",
        "control",
        "test",
        ratio_definition,
        0.05,
        0.8,
        prepared_metric_context=prepared_invalid,
    )
    legacy_invalid_row = rows_module._build_metric_row(
        invalid_frame,
        "group",
        "control",
        "test",
        ratio_definition,
        0.05,
        0.8,
    )
    for row in (prepared_invalid_row, legacy_invalid_row):
        assert math.isnan(row["delta_abs"])
        assert math.isnan(row["variance_group_2"])
        assert math.isnan(row["variance_group_1"])
        assert math.isnan(row["s.e."])

    valid_frame = invalid_frame.assign(
        num=[1.0, 4.0, 3.0, 8.0],
        den=[1.0, 2.0, 1.0, 2.0],
    )
    valid_row = rows_module._build_metric_row(
        valid_frame,
        "group",
        "control",
        "test",
        ratio_definition,
        0.05,
        0.8,
    )
    assert math.isfinite(valid_row["delta_abs"])
    assert math.isfinite(valid_row["variance_group_2"])
    assert math.isfinite(valid_row["variance_group_1"])
    assert math.isfinite(valid_row["s.e."])
