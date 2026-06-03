from __future__ import annotations

import importlib

import numpy as np
import pandas as pd
import pytest

from analytics_toolkit.ab_utils import format_ab_metrics


def _build_metric_rows() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "segment": ["first", "first"],
            "group_1": ["test", "test"],
            "group_2": ["control", "control"],
            "metric_name": ["orders", "gmv"],
            "metric_control": [10.0, 100.0],
            "metric_test": [12.0, 110.0],
            "n0": [3, 3],
            "n1": [4, 4],
            "outliers_cutoff": [99.0, 999.0],
            "outliers_n_control": [0, 1],
            "outliers_n_test": [1, 2],
            "variance_control": [1.5, 10.0],
            "variance_test": [2.5, 12.0],
            "delta_abs": [2.0, 10.0],
            "delta_relative": [0.2, 0.1],
            "mde_abs": [3.0, 30.0],
            "mde_relative": [0.3, 0.3],
            "s.e.": [0.5, 5.0],
            "p-value": [0.04, 0.2],
            "s.e. CUPED": [0.4, 4.0],
            "p-value CUPED": [0.03, 0.15],
            "mde_abs CUPED": [2.4, 24.0],
            "mde_relative CUPED": [0.24, 0.24],
            "s.e. bootstrap": [0.6, 6.0],
            "bootstrap_adj_p": [0.08, 0.3],
        }
    )


def test_format_ab_metrics_defaults_to_metric_value_table() -> None:
    result = format_ab_metrics(_build_metric_rows())

    expected = pd.DataFrame(
        {
            "metric": ["group_size", "orders", "gmv"],
            "control": [3.0, 10.0, 100.0],
            "test": [4.0, 12.0, 110.0],
        }
    )
    pd.testing.assert_frame_equal(result, expected)


def test_format_ab_metrics_accepts_consistent_repeated_group_values() -> None:
    df = pd.DataFrame(
        {
            "group_1": ["test_1", "test_2"],
            "group_2": ["control", "control"],
            "metric_name": ["orders", "orders"],
            "metric_control": [10.0, 10.0],
            "metric_test": [12.0, 13.0],
            "n0": [100, 100],
            "n1": [120, 130],
        }
    )

    result = format_ab_metrics(df)

    expected = pd.DataFrame(
        {
            "metric": ["group_size", "orders"],
            "control": [100.0, 10.0],
            "test_1": [120.0, 12.0],
            "test_2": [130.0, 13.0],
        }
    )
    pd.testing.assert_frame_equal(result, expected)


def test_format_ab_metrics_allows_configured_repeated_group_values() -> None:
    df = pd.DataFrame(
        {
            "group_1": ["test_1", "test_2"],
            "group_2": ["control", "control"],
            "metric_name": ["orders", "orders"],
            "metric_control": [10.0, 10.000000000001],
            "metric_test": [12.0, 13.0],
            "n0": [100, 100],
            "n1": [120, 130],
        }
    )

    result = format_ab_metrics(df, allow_repeated_groups=["control"])

    expected = pd.DataFrame(
        {
            "metric": ["group_size", "orders"],
            "control": [100.0, 10.0],
            "test_1": [120.0, 12.0],
            "test_2": [130.0, 13.0],
        }
    )
    pd.testing.assert_frame_equal(result, expected)


def test_format_ab_metrics_allows_configured_repeated_group_values_for_group_outputs() -> None:
    df = pd.DataFrame(
        {
            "group_1": ["test_1", "test_2"],
            "group_2": ["control", "control"],
            "metric_name": ["orders", "orders"],
            "metric_control": [10.0, 11.0],
            "metric_test": [12.0, 13.0],
            "n0": [100, 100],
            "n1": [120, 130],
            "variance_control": [1.5, 1.6],
            "variance_test": [2.5, 2.6],
        }
    )

    result = format_ab_metrics(
        df,
        output_type=["metric_values", "n", "variance"],
        allow_repeated_groups=["control"],
    )

    expected = pd.DataFrame(
        {
            "metric": ["group_size", "orders"],
            "control_metric_value": [100.0, 10.0],
            "test_1_metric_value": [120.0, 12.0],
            "test_2_metric_value": [130.0, 13.0],
            "control_n": [np.nan, 100.0],
            "test_1_n": [np.nan, 120.0],
            "test_2_n": [np.nan, 130.0],
            "control_variance": [np.nan, 1.5],
            "test_1_variance": [np.nan, 2.5],
            "test_2_variance": [np.nan, 2.6],
        }
    )
    pd.testing.assert_frame_equal(result, expected)


def test_format_ab_metrics_rejects_conflicting_group_size_for_repeated_groups() -> None:
    df = pd.DataFrame(
        {
            "group_1": ["test_1", "test_2"],
            "group_2": ["control", "control"],
            "metric_name": ["orders", "orders"],
            "metric_control": [10.0, 11.0],
            "metric_test": [12.0, 13.0],
            "n0": [100, 101],
            "n1": [120, 130],
        }
    )

    with pytest.raises(ValueError, match="Conflicting group size"):
        format_ab_metrics(df, allow_repeated_groups=["control"])


def test_format_ab_metrics_rejects_unconfigured_repeated_group_values() -> None:
    df = pd.DataFrame(
        {
            "group_1": ["test_1", "test_2"],
            "group_2": ["control", "control"],
            "metric_name": ["orders", "orders"],
            "metric_control": [10.0, 11.0],
            "metric_test": [12.0, 13.0],
            "n0": [100, 100],
            "n1": [120, 130],
        }
    )

    with pytest.raises(ValueError, match="Duplicate formatted output cell"):
        format_ab_metrics(df)


def test_format_ab_metrics_rejects_repeated_values_for_groups_not_configured() -> None:
    df = pd.DataFrame(
        {
            "group_1": ["test", "test"],
            "group_2": ["control_1", "control_2"],
            "metric_name": ["orders", "orders"],
            "metric_control": [10.0, 11.0],
            "metric_test": [12.0, 13.0],
            "n0": [100, 110],
            "n1": [120, 120],
        }
    )

    with pytest.raises(ValueError, match="Duplicate formatted output cell"):
        format_ab_metrics(df, allow_repeated_groups=["control_1", "control_2"])


def test_format_ab_metrics_keeps_labels_and_first_seen_order() -> None:
    df = pd.DataFrame(
        {
            "segment": ["B", "A", "B"],
            "country": ["RU", "KZ", "RU"],
            "group_1": ["variant_b", "variant_a", "variant_b"],
            "group_2": ["control", "control", "control"],
            "metric_name": ["orders", "orders", "gmv"],
            "metric_control": [10.0, 20.0, 100.0],
            "metric_test": [12.0, 22.0, 115.0],
            "n0": [100, 200, 100],
            "n1": [120, 220, 120],
        }
    )

    result = format_ab_metrics(df, label_cols=["segment", "country"])

    expected = pd.DataFrame(
        {
            "segment": ["B", "B", "B", "A", "A"],
            "country": ["RU", "RU", "RU", "KZ", "KZ"],
            "metric": ["group_size", "orders", "gmv", "group_size", "orders"],
            "control": [100.0, 10.0, 100.0, 200.0, 20.0],
            "variant_b": [120.0, 12.0, 115.0, np.nan, np.nan],
            "variant_a": [np.nan, np.nan, np.nan, 220.0, 22.0],
        }
    )
    pd.testing.assert_frame_equal(result, expected)


def test_format_ab_metrics_supports_multiple_output_types() -> None:
    result = format_ab_metrics(
        _build_metric_rows().iloc[[0]],
        output_type=["metric_values", "variance", "n", "p_values", "delta_abs", "se"],
    )

    expected = pd.DataFrame(
        {
            "metric": ["group_size", "orders"],
            "control_metric_value": [3.0, 10.0],
            "test_metric_value": [4.0, 12.0],
            "control_variance": [np.nan, 1.5],
            "test_variance": [np.nan, 2.5],
            "control_n": [np.nan, 3.0],
            "test_n": [np.nan, 4.0],
            "test_vs_control_p_value": [np.nan, 0.04],
            "test_vs_control_delta_abs": [np.nan, 2.0],
            "test_vs_control_se": [np.nan, 0.5],
        }
    )
    pd.testing.assert_frame_equal(result, expected)


def test_format_ab_metrics_adds_group_size_columns_for_comparison_outputs() -> None:
    result = format_ab_metrics(
        _build_metric_rows().iloc[[0]],
        output_type=["p_values"],
    )

    expected = pd.DataFrame(
        {
            "metric": ["group_size", "orders"],
            "control": [3.0, np.nan],
            "test": [4.0, np.nan],
            "test_vs_control_p_value": [np.nan, 0.04],
        }
    )
    pd.testing.assert_frame_equal(result, expected)


def test_format_ab_metrics_supports_cuped_mde_outputs() -> None:
    result = format_ab_metrics(
        _build_metric_rows().iloc[[0]],
        output_type=["mde_abs_cuped", "mde_relative_cuped"],
    )

    expected = pd.DataFrame(
        {
            "metric": ["group_size", "orders"],
            "control": [3.0, np.nan],
            "test": [4.0, np.nan],
            "test_vs_control_mde_abs_cuped": [np.nan, 2.4],
            "test_vs_control_mde_relative_cuped": [np.nan, 0.24],
        }
    )
    pd.testing.assert_frame_equal(result, expected)


def test_format_ab_metrics_accepts_single_output_type_string() -> None:
    df = _build_metric_rows()

    result = format_ab_metrics(df, output_type="delta_relative")
    expected = format_ab_metrics(df, output_type=["delta_relative"])

    pd.testing.assert_frame_equal(result, expected)


def test_format_ab_metrics_keeps_simple_group_names_for_single_comparison_output() -> None:
    df = pd.DataFrame(
        {
            "group_1": ["test_1", "test_2"],
            "group_2": ["control", "control"],
            "metric_name": ["orders", "orders"],
            "metric_control": [10.0, 10.0],
            "metric_test": [12.0, 13.0],
            "delta_relative": [0.2, 0.3],
            "n0": [100, 100],
            "n1": [120, 130],
        }
    )

    result = format_ab_metrics(
        df,
        output_type=["delta_relative"],
        keep_simple_group_names=True,
    )

    expected = pd.DataFrame(
        {
            "metric": ["group_size", "orders"],
            "control": [100.0, np.nan],
            "test_1": [120.0, 0.2],
            "test_2": [130.0, 0.3],
        }
    )
    pd.testing.assert_frame_equal(result, expected)


def test_format_ab_metrics_supports_significant_delta_outputs() -> None:
    result = format_ab_metrics(
        _build_metric_rows(),
        output_type=["delta_relative_significant", "delta_absolute_significant"],
        significance_alpha=0.05,
        significance_p_value="p_values",
    )

    expected = pd.DataFrame(
        {
            "metric": ["group_size", "orders", "gmv"],
            "control": [3.0, np.nan, np.nan],
            "test": [4.0, np.nan, np.nan],
            "test_vs_control_delta_relative_significant": [np.nan, 0.2, np.nan],
            "test_vs_control_delta_absolute_significant": [np.nan, 2.0, np.nan],
        }
    )
    pd.testing.assert_frame_equal(result, expected)


def test_format_ab_metrics_keeps_simple_group_names_for_significant_delta_output() -> None:
    df = _build_metric_rows()
    df["group_1"] = ["test_1", "test_2"]
    df["metric_name"] = ["orders", "orders"]
    df["delta_relative"] = [0.2, 0.3]
    df["p-value"] = [0.04, 0.2]

    result = format_ab_metrics(
        df,
        output_type=["delta_relative_significant"],
        significance_alpha=0.05,
        significance_p_value="p_values",
        keep_simple_group_names=True,
    )

    expected = pd.DataFrame(
        {
            "metric": ["group_size", "orders"],
            "control": [3.0, np.nan],
            "test_1": [4.0, 0.2],
            "test_2": [4.0, np.nan],
        }
    )
    pd.testing.assert_frame_equal(result, expected)


def test_format_ab_metrics_rejects_ambiguous_simple_group_names() -> None:
    with pytest.raises(ValueError, match="Duplicate formatted output column"):
        format_ab_metrics(
            _build_metric_rows().iloc[[0]],
            output_type=["p_values", "delta_relative"],
            keep_simple_group_names=True,
        )


@pytest.mark.parametrize(
    ("significance_p_value", "expected_values"),
    [
        ("p_values", [0.2, np.nan]),
        ("p_values_cuped", [np.nan, 0.1]),
        ("p_values_adj", [0.2, np.nan]),
    ],
)
def test_format_ab_metrics_uses_configured_significance_p_value_source(
    significance_p_value: str,
    expected_values: list[float],
) -> None:
    df = _build_metric_rows()
    df["p-value"] = [0.04, 0.2]
    df["p-value CUPED"] = [0.2, 0.04]
    df["bootstrap_adj_p"] = [0.01, 0.2]

    result = format_ab_metrics(
        df,
        output_type=["delta_relative_significant"],
        significance_alpha=0.05,
        significance_p_value=significance_p_value,
    )

    expected = pd.DataFrame(
        {
            "metric": ["group_size", "orders", "gmv"],
            "control": [3.0, np.nan, np.nan],
            "test": [4.0, np.nan, np.nan],
            "test_vs_control_delta_relative_significant": [
                np.nan,
                *expected_values,
            ],
        }
    )
    pd.testing.assert_frame_equal(result, expected)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"significance_p_value": "p_values"},
        {"significance_alpha": 0.05},
    ],
)
def test_format_ab_metrics_requires_significance_configuration(
    kwargs: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="required"):
        format_ab_metrics(
            _build_metric_rows(),
            output_type=["delta_relative_significant"],
            **kwargs,
        )


def test_format_ab_metrics_validates_significance_configuration() -> None:
    with pytest.raises(ValueError, match="significance_alpha"):
        format_ab_metrics(
            _build_metric_rows(),
            output_type=["delta_relative_significant"],
            significance_alpha=1.0,
            significance_p_value="p_values",
        )

    with pytest.raises(ValueError, match="significance_p_value"):
        format_ab_metrics(
            _build_metric_rows(),
            output_type=["delta_relative_significant"],
            significance_alpha=0.05,
            significance_p_value="unknown",
        )


def test_format_ab_metrics_raises_for_missing_significance_p_value_source() -> None:
    df = _build_metric_rows().drop(columns=["p-value CUPED"])

    with pytest.raises(ValueError, match="Missing source column"):
        format_ab_metrics(
            df,
            output_type=["delta_relative_significant"],
            significance_alpha=0.05,
            significance_p_value="p_values_cuped",
        )


def test_format_ab_metrics_raises_for_duplicate_output_cells() -> None:
    df = pd.concat(
        [_build_metric_rows().iloc[[0]], _build_metric_rows().iloc[[0]]],
        ignore_index=True,
    )

    with pytest.raises(ValueError, match="Duplicate formatted output cell"):
        format_ab_metrics(df)


def test_format_ab_metrics_raises_for_missing_required_columns() -> None:
    df = _build_metric_rows().drop(columns=["group_1"])

    with pytest.raises(ValueError, match="Missing required column"):
        format_ab_metrics(df)


def test_format_ab_metrics_raises_for_missing_requested_optional_columns() -> None:
    df = _build_metric_rows().drop(columns=["p-value CUPED"])

    with pytest.raises(ValueError, match="Missing source column"):
        format_ab_metrics(df, output_type=["p_values_cuped"])


def test_format_ab_metrics_validates_output_type() -> None:
    with pytest.raises(ValueError, match="Unsupported output_type"):
        format_ab_metrics(_build_metric_rows(), output_type=["metric_values", "unknown"])


def test_format_ab_metrics_validates_allow_repeated_groups() -> None:
    with pytest.raises(ValueError, match="allow_repeated_groups"):
        format_ab_metrics(
            _build_metric_rows(),
            allow_repeated_groups="control",  # type: ignore[arg-type]
        )

    with pytest.raises(ValueError, match="allow_repeated_groups"):
        format_ab_metrics(
            _build_metric_rows(),
            allow_repeated_groups=["control", 1],  # type: ignore[list-item]
        )

    with pytest.raises(ValueError, match="allow_repeated_groups"):
        format_ab_metrics(
            _build_metric_rows(),
            allow_repeated_groups=["control", "control"],
        )


def test_format_ab_metrics_validates_keep_simple_group_names() -> None:
    with pytest.raises(ValueError, match="keep_simple_group_names"):
        format_ab_metrics(
            _build_metric_rows(),
            keep_simple_group_names="yes",  # type: ignore[arg-type]
        )


def test_format_ab_metrics_is_publicly_reexported() -> None:
    ab_utils_module = importlib.import_module("analytics_toolkit.ab_utils")
    metrics_module = importlib.import_module("analytics_toolkit.ab_utils.metrics")
    formatter_module = importlib.import_module("analytics_toolkit.ab_utils.formatter")

    assert ab_utils_module.format_ab_metrics is formatter_module.format_ab_metrics
    assert metrics_module.format_ab_metrics is formatter_module.format_ab_metrics
