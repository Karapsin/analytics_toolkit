from __future__ import annotations

from tests.ab_utils._support.edge_cases import (
    math,
    outliers_module,
    pd,
    pytest,
)


def test_outlier_context_missing_columns_and_empty_cutoff_paths() -> None:
    mean_metric = {"kind": "mean", "metric_key": "metric", "column": "metric"}
    assert (
        outliers_module._build_outlier_context(
            pd.DataFrame({"other": [1.0]}), mean_metric, 0.99, "truncate", allow_missing=True
        )
        is None
    )
    with pytest.raises(KeyError, match="metric"):
        outliers_module._build_outlier_context(
            pd.DataFrame({"other": [1.0]}), mean_metric, 0.99, "truncate"
        )

    ratio_metric = {
        "kind": "ratio",
        "metric_key": "ratio",
        "ratio_spec": {"numerator": "num", "denominator": "den", "level": "agg"},
    }
    assert (
        outliers_module._build_outlier_context(
            pd.DataFrame({"num": [1.0]}),
            ratio_metric,
            0.99,
            "truncate",
            allow_missing=True,
        )
        is None
    )
    with pytest.raises(KeyError, match="den"):
        outliers_module._build_outlier_context(
            pd.DataFrame({"num": [1.0]}), ratio_metric, 0.99, "truncate"
        )
    assert math.isnan(
        outliers_module._compute_outlier_cutoff(
            pd.Series([0.0, math.nan]), 0.99, "non_zero_truncate"
        )
    )


def test_outlier_context_collection_handles_zero_one_and_multiple_metrics() -> None:
    frame = pd.DataFrame({"first": [1.0, 2.0], "second": [2.0, 4.0]})
    definitions = [
        {"kind": "mean", "metric_key": name, "column": name} for name in ("first", "second")
    ]

    assert outliers_module._build_outlier_contexts(frame, [], 1.0, "truncate") == {}
    assert list(
        outliers_module._build_outlier_contexts(frame, definitions[:1], 1.0, "truncate")
    ) == ["first"]
    assert list(outliers_module._build_outlier_contexts(frame, definitions, 1.0, "truncate")) == [
        "first",
        "second",
    ]
    missing_then_present = [
        {"kind": "mean", "metric_key": "missing", "column": "missing"},
        definitions[0],
    ]
    assert list(
        outliers_module._build_outlier_contexts(
            frame,
            missing_then_present,
            1.0,
            "truncate",
            allow_missing=True,
        )
    ) == ["first"]


def test_outlier_masks_and_removal_policy_edges() -> None:
    values = pd.Series([1.0, 10.0])
    no_context = outliers_module._build_value_outlier_mask(values, None)
    nan_context = outliers_module._build_value_outlier_mask(
        values, {"cutoff": math.nan, "policy": "truncate"}
    )
    assert not no_context.any()
    assert not nan_context.any()

    transformed, mask = outliers_module._apply_outliers_to_values(
        values, {"cutoff": 2.0, "policy": "remove"}
    )
    assert mask.tolist() == [False, True]
    assert math.isnan(transformed.iloc[1])

    numerator = pd.Series([1.0, 10.0])
    denominator = pd.Series([1.0, 1.0])
    assert not outliers_module._build_agg_ratio_outlier_mask(numerator, denominator, None).any()
    assert not outliers_module._build_agg_ratio_outlier_mask(
        numerator, denominator, {"cutoff": math.nan, "policy": "truncate"}
    ).any()
    transformed_num, transformed_den, ratio_mask = (
        outliers_module._apply_outliers_to_agg_ratio_components(
            numerator,
            denominator,
            {"cutoff": 2.0, "policy": "remove"},
        )
    )
    assert ratio_mask.tolist() == [False, True]
    assert math.isnan(transformed_num.iloc[1])
    assert math.isnan(transformed_den.iloc[1])
    assert math.isnan(outliers_module._get_outlier_cutoff(None))
