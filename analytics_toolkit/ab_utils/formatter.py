from __future__ import annotations

from collections.abc import Sequence
from numbers import Real
from typing import Any

import pandas as pd

_REQUIRED_COLUMNS = frozenset(
    {
        "metric_name",
        "group_1",
        "group_2",
        "metric_group_1",
        "metric_group_2",
        "n_group_1",
        "n_group_2",
    }
)

_GROUP_OUTPUTS = {
    "metric_values": (
        ("group_1", "metric_group_1", "metric_value"),
        ("group_2", "metric_group_2", "metric_value"),
    ),
    "n": (
        ("group_1", "n_group_1", "n"),
        ("group_2", "n_group_2", "n"),
    ),
    "outliers_n": (
        ("group_1", "outliers_n_group_1", "outliers_n"),
        ("group_2", "outliers_n_group_2", "outliers_n"),
    ),
    "variance": (
        ("group_1", "variance_group_1", "variance"),
        ("group_2", "variance_group_2", "variance"),
    ),
}

_COMPARISON_OUTPUTS = {
    "p_values": ("p-value", "p_value"),
    "p_values_cuped": ("p-value CUPED", "p_value_cuped"),
    "p_values_adj": ("bootstrap_adj_p", "p_value_adj"),
    "delta_abs": ("delta_abs", "delta_abs"),
    "delta_relative": ("delta_relative", "delta_relative"),
    "mde_abs": ("mde_abs", "mde_abs"),
    "mde_relative": ("mde_relative", "mde_relative"),
    "mde_abs_cuped": ("mde_abs CUPED", "mde_abs_cuped"),
    "mde_relative_cuped": ("mde_relative CUPED", "mde_relative_cuped"),
    "se": ("s.e.", "se"),
    "se_cuped": ("s.e. CUPED", "se_cuped"),
    "se_bootstrap": ("s.e. bootstrap", "se_bootstrap"),
    "outliers_cutoff": ("outliers_cutoff", "outliers_cutoff"),
}

_SIGNIFICANT_DELTA_OUTPUTS = {
    "delta_relative_significant": ("delta_relative", "delta_relative_significant"),
    "delta_absolute_significant": ("delta_abs", "delta_absolute_significant"),
}

_SIGNIFICANCE_P_VALUE_COLUMNS = {
    "p_values": _COMPARISON_OUTPUTS["p_values"][0],
    "p_values_cuped": _COMPARISON_OUTPUTS["p_values_cuped"][0],
    "p_values_adj": _COMPARISON_OUTPUTS["p_values_adj"][0],
}

_SUPPORTED_OUTPUTS = (
    frozenset(_GROUP_OUTPUTS)
    | frozenset(_COMPARISON_OUTPUTS)
    | frozenset(_SIGNIFICANT_DELTA_OUTPUTS)
)


def format_ab_metrics(
    df: pd.DataFrame,
    label_cols: list[str] | None = None,
    output_type: str | list[str] | None = None,
    significance_alpha: float | None = None,
    significance_p_value: str | None = None,
    allow_repeated_groups: list[str] | None = None,
    keep_simple_group_names: bool = False,
) -> pd.DataFrame:
    """Format AB metric comparison rows into a wide presentation dataframe."""
    labels = _validate_label_cols(df, label_cols)
    outputs = _validate_output_type(output_type)
    repeated_groups = _validate_allow_repeated_groups(allow_repeated_groups)
    simple_group_names = _validate_keep_simple_group_names(keep_simple_group_names)
    significance_source_column = _validate_significance_options(
        outputs=outputs,
        significance_alpha=significance_alpha,
        significance_p_value=significance_p_value,
    )
    _validate_source_columns(
        df=df,
        label_cols=labels,
        outputs=outputs,
        significance_source_column=significance_source_column,
    )

    group_order = _ordered_groups(df)
    comparison_order = _ordered_comparisons(df)
    metric_output_columns = _build_output_columns(
        outputs=outputs,
        groups=group_order,
        comparisons=comparison_order,
        keep_simple_group_names=simple_group_names,
    )
    group_size_columns, group_size_value_columns = _build_group_size_columns(
        outputs=outputs,
        groups=group_order,
        metric_output_columns=metric_output_columns,
    )
    output_columns = [*group_size_columns, *metric_output_columns]
    _validate_output_columns(labels, output_columns)

    label_order: list[tuple[Any, ...]] = []
    row_order_by_label: dict[tuple[Any, ...], list[tuple[Any, ...]]] = {}
    rows_by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
    group_size_rows_by_label: dict[tuple[Any, ...], dict[str, Any]] = {}
    assigned_cells: set[tuple[tuple[Any, ...], str]] = set()
    seen_comparisons: set[tuple[Any, ...]] = set()

    for _, source_row in df.iterrows():
        label_key = _label_key(source_row, labels)
        if label_key not in group_size_rows_by_label:
            label_order.append(label_key)
            row_order_by_label[label_key] = []
            group_size_rows_by_label[label_key] = {
                **{column: source_row[column] for column in labels},
                "metric": "group_size",
            }
        group_size_row = group_size_rows_by_label[label_key]
        for group_col, value_col in (
            ("group_1", "n_group_1"),
            ("group_2", "n_group_2"),
        ):
            group_name = _column_part(source_row[group_col])
            _set_group_size_value(
                label_key=label_key,
                target_row=group_size_row,
                group_name=group_name,
                output_column=group_size_value_columns[group_name],
                value=source_row[value_col],
            )

        row_key = _row_key(source_row, labels)
        comparison_key = (
            *row_key,
            _column_part(source_row["group_1"]),
            _column_part(source_row["group_2"]),
        )
        if comparison_key in seen_comparisons:
            raise ValueError(
                "Duplicate formatted output cell for "
                f"metric {row_key[-1]!r} and comparison "
                f"{comparison_key[-2]!r} vs {comparison_key[-1]!r}."
            )
        seen_comparisons.add(comparison_key)

        if row_key not in rows_by_key:
            row_order_by_label[label_key].append(row_key)
            rows_by_key[row_key] = {
                **{column: source_row[column] for column in labels},
                "metric": source_row["metric_name"],
            }

        target_row = rows_by_key[row_key]
        for output in outputs:
            if output in _GROUP_OUTPUTS:
                for group_col, value_col, suffix in _GROUP_OUTPUTS[output]:
                    group_name = _column_part(source_row[group_col])
                    output_column = _group_output_column(
                        group_name=group_name,
                        suffix=suffix,
                        plain_metric_values=outputs == ["metric_values"],
                    )
                    _set_output_value(
                        row_key=row_key,
                        target_row=target_row,
                        assigned_cells=assigned_cells,
                        output_column=output_column,
                        value=source_row[value_col],
                        allow_existing_any=group_name in repeated_groups,
                        allow_existing_equal=True,
                    )
            elif output in _COMPARISON_OUTPUTS:
                value_col, suffix = _COMPARISON_OUTPUTS[output]
                output_column = _comparison_output_column(
                    test_group=_column_part(source_row["group_1"]),
                    baseline_group=_column_part(source_row["group_2"]),
                    suffix=suffix,
                    keep_simple_group_names=simple_group_names,
                )
                _set_output_value(
                    row_key=row_key,
                    target_row=target_row,
                    assigned_cells=assigned_cells,
                    output_column=output_column,
                    value=source_row[value_col],
                    allow_existing_any=False,
                    allow_existing_equal=False,
                )
            else:
                value_col, suffix = _SIGNIFICANT_DELTA_OUTPUTS[output]
                output_column = _comparison_output_column(
                    test_group=_column_part(source_row["group_1"]),
                    baseline_group=_column_part(source_row["group_2"]),
                    suffix=suffix,
                    keep_simple_group_names=simple_group_names,
                )
                value = _significant_delta_value(
                    source_row=source_row,
                    value_col=value_col,
                    significance_source_column=significance_source_column,
                    significance_alpha=significance_alpha,
                )
                _set_output_value(
                    row_key=row_key,
                    target_row=target_row,
                    assigned_cells=assigned_cells,
                    output_column=output_column,
                    value=value,
                    allow_existing_any=False,
                    allow_existing_equal=False,
                )

    columns = [*labels, "metric", *output_columns]
    formatted_rows: list[dict[str, Any]] = []
    for label_key in label_order:
        formatted_rows.append(group_size_rows_by_label[label_key])
        formatted_rows.extend(rows_by_key[row_key] for row_key in row_order_by_label[label_key])
    return pd.DataFrame(formatted_rows, columns=columns)


def _validate_label_cols(df: pd.DataFrame, label_cols: list[str] | None) -> list[str]:
    if label_cols is None:
        return []
    if not isinstance(label_cols, list):
        raise ValueError("label_cols must be a list of column names or None.")
    if len(set(label_cols)) != len(label_cols):
        raise ValueError("label_cols must not contain duplicates.")
    invalid_labels = [column for column in label_cols if not isinstance(column, str)]
    if invalid_labels:
        raise ValueError("label_cols must contain only column names.")
    missing = [column for column in label_cols if column not in df.columns]
    if missing:
        raise ValueError(f"Missing label column(s): {', '.join(missing)}.")
    return list(label_cols)


def _validate_output_type(output_type: str | list[str] | None) -> list[str]:
    if output_type is None:
        outputs = ["metric_values"]
    elif isinstance(output_type, str):
        outputs = [output_type]
    else:
        outputs = output_type
    if not isinstance(outputs, list) or not outputs:
        raise ValueError(
            "output_type must be an output name, a non-empty list of output names, or None."
        )
    invalid_outputs = [
        output
        for output in outputs
        if not isinstance(output, str) or output not in _SUPPORTED_OUTPUTS
    ]
    if invalid_outputs:
        supported = ", ".join(sorted(_SUPPORTED_OUTPUTS))
        invalid = ", ".join(str(output) for output in invalid_outputs)
        raise ValueError(f"Unsupported output_type value(s): {invalid}. Supported: {supported}.")
    if len(set(outputs)) != len(outputs):
        raise ValueError("output_type must not contain duplicates.")
    return list(outputs)


def _validate_allow_repeated_groups(
    allow_repeated_groups: list[str] | None,
) -> set[str]:
    if allow_repeated_groups is None:
        return set()
    if not isinstance(allow_repeated_groups, list):
        raise ValueError("allow_repeated_groups must be a list of group names or None.")
    invalid_groups = [
        group for group in allow_repeated_groups if not isinstance(group, str)
    ]
    if invalid_groups:
        raise ValueError("allow_repeated_groups must contain only group names.")
    if len(set(allow_repeated_groups)) != len(allow_repeated_groups):
        raise ValueError("allow_repeated_groups must not contain duplicates.")
    return set(allow_repeated_groups)


def _validate_keep_simple_group_names(keep_simple_group_names: bool) -> bool:
    if not isinstance(keep_simple_group_names, bool):
        raise ValueError("keep_simple_group_names must be a boolean.")
    return keep_simple_group_names


def _validate_significance_options(
    *,
    outputs: Sequence[str],
    significance_alpha: float | None,
    significance_p_value: str | None,
) -> str | None:
    significant_outputs = [
        output for output in outputs if output in _SIGNIFICANT_DELTA_OUTPUTS
    ]
    if not significant_outputs:
        return None

    if significance_alpha is None:
        raise ValueError(
            "significance_alpha is required when requesting significant delta outputs."
        )
    if (
        not isinstance(significance_alpha, Real)
        or isinstance(significance_alpha, bool)
        or not 0 < significance_alpha < 1
    ):
        raise ValueError("significance_alpha must be numeric and between 0 and 1.")

    if significance_p_value is None:
        raise ValueError(
            "significance_p_value is required when requesting significant delta outputs."
        )
    if significance_p_value not in _SIGNIFICANCE_P_VALUE_COLUMNS:
        supported = ", ".join(sorted(_SIGNIFICANCE_P_VALUE_COLUMNS))
        raise ValueError(f"significance_p_value must be one of: {supported}.")

    return _SIGNIFICANCE_P_VALUE_COLUMNS[significance_p_value]


def _validate_source_columns(
    df: pd.DataFrame,
    label_cols: Sequence[str],
    outputs: Sequence[str],
    significance_source_column: str | None,
) -> None:
    missing_required = sorted(_REQUIRED_COLUMNS.difference(df.columns))
    if missing_required:
        raise ValueError(f"Missing required column(s): {', '.join(missing_required)}.")

    source_columns: set[str] = set(label_cols)
    for output in outputs:
        if output in _GROUP_OUTPUTS:
            source_columns.update(
                value_col for _group_col, value_col, _suffix in _GROUP_OUTPUTS[output]
            )
        elif output in _COMPARISON_OUTPUTS:
            source_columns.add(_COMPARISON_OUTPUTS[output][0])
        else:
            source_columns.add(_SIGNIFICANT_DELTA_OUTPUTS[output][0])
    if significance_source_column is not None:
        source_columns.add(significance_source_column)

    missing_sources = sorted(column for column in source_columns if column not in df.columns)
    if missing_sources:
        raise ValueError(
            "Missing source column(s) for requested output_type: "
            f"{', '.join(missing_sources)}."
        )


def _ordered_groups(df: pd.DataFrame) -> list[str]:
    groups: list[str] = []
    seen: set[str] = set()
    for _, row in df.iterrows():
        for column in ("group_2", "group_1"):
            group_name = _column_part(row[column])
            if group_name not in seen:
                seen.add(group_name)
                groups.append(group_name)
    return groups


def _ordered_comparisons(df: pd.DataFrame) -> list[tuple[str, str]]:
    comparisons: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for _, row in df.iterrows():
        comparison = (_column_part(row["group_1"]), _column_part(row["group_2"]))
        if comparison not in seen:
            seen.add(comparison)
            comparisons.append(comparison)
    return comparisons


def _build_output_columns(
    outputs: Sequence[str],
    groups: Sequence[str],
    comparisons: Sequence[tuple[str, str]],
    keep_simple_group_names: bool,
) -> list[str]:
    plain_metric_values = list(outputs) == ["metric_values"]
    columns: list[str] = []
    for output in outputs:
        if output in _GROUP_OUTPUTS:
            suffix = _GROUP_OUTPUTS[output][0][2]
            columns.extend(
                _group_output_column(
                    group_name=group_name,
                    suffix=suffix,
                    plain_metric_values=plain_metric_values,
                )
                for group_name in groups
            )
        elif output in _COMPARISON_OUTPUTS:
            _source_column, suffix = _COMPARISON_OUTPUTS[output]
            columns.extend(
                _comparison_output_column(
                    test_group=test_group,
                    baseline_group=baseline_group,
                    suffix=suffix,
                    keep_simple_group_names=keep_simple_group_names,
                )
                for test_group, baseline_group in comparisons
            )
        else:
            _source_column, suffix = _SIGNIFICANT_DELTA_OUTPUTS[output]
            columns.extend(
                _comparison_output_column(
                    test_group=test_group,
                    baseline_group=baseline_group,
                    suffix=suffix,
                    keep_simple_group_names=keep_simple_group_names,
                )
                for test_group, baseline_group in comparisons
            )
    return columns


def _build_group_size_columns(
    outputs: Sequence[str],
    groups: Sequence[str],
    metric_output_columns: Sequence[str],
) -> tuple[list[str], dict[str, str]]:
    if "metric_values" in outputs:
        plain_metric_values = list(outputs) == ["metric_values"]
        return [], {
            group_name: _group_output_column(
                group_name=group_name,
                suffix="metric_value",
                plain_metric_values=plain_metric_values,
            )
            for group_name in groups
        }

    metric_output_column_set = set(metric_output_columns)
    extra_columns = [
        group_name for group_name in groups if group_name not in metric_output_column_set
    ]
    return extra_columns, {group_name: group_name for group_name in groups}


def _validate_output_columns(label_cols: Sequence[str], output_columns: Sequence[str]) -> None:
    leading_columns = [*label_cols, "metric"]
    all_columns = [*leading_columns, *output_columns]
    if len(set(all_columns)) != len(all_columns):
        duplicates = sorted(
            column
            for column in set(all_columns)
            if all_columns.count(column) > 1
        )
        raise ValueError(f"Duplicate formatted output column(s): {', '.join(duplicates)}.")


def _label_key(row: pd.Series, label_cols: Sequence[str]) -> tuple[Any, ...]:
    return tuple(row[column] for column in label_cols)


def _row_key(row: pd.Series, label_cols: Sequence[str]) -> tuple[Any, ...]:
    return (*_label_key(row, label_cols), row["metric_name"])


def _column_part(value: Any) -> str:
    return str(value)


def _group_output_column(
    *,
    group_name: str,
    suffix: str,
    plain_metric_values: bool,
) -> str:
    if plain_metric_values:
        return group_name
    return f"{group_name}_{suffix}"


def _comparison_output_column(
    *,
    test_group: str,
    baseline_group: str,
    suffix: str,
    keep_simple_group_names: bool,
) -> str:
    if keep_simple_group_names:
        return test_group
    return f"{test_group}_vs_{baseline_group}_{suffix}"


def _set_output_value(
    *,
    row_key: tuple[Any, ...],
    target_row: dict[str, Any],
    assigned_cells: set[tuple[tuple[Any, ...], str]],
    output_column: str,
    value: Any,
    allow_existing_any: bool,
    allow_existing_equal: bool,
) -> None:
    cell_key = (row_key, output_column)
    if cell_key in assigned_cells:
        if allow_existing_any:
            return
        if allow_existing_equal and _values_equal(target_row[output_column], value):
            return
        raise ValueError(
            "Duplicate formatted output cell for "
            f"metric {row_key[-1]!r} and column {output_column!r}."
        )
    assigned_cells.add(cell_key)
    target_row[output_column] = value


def _set_group_size_value(
    *,
    label_key: tuple[Any, ...],
    target_row: dict[str, Any],
    group_name: str,
    output_column: str,
    value: Any,
) -> None:
    if output_column in target_row:
        if _values_equal(target_row[output_column], value):
            return
        label_summary = ", ".join(str(part) for part in label_key) or "<all>"
        raise ValueError(
            "Conflicting group size for "
            f"label group {label_summary!r} and group {group_name!r}."
        )
    target_row[output_column] = value


def _significant_delta_value(
    *,
    source_row: pd.Series,
    value_col: str,
    significance_source_column: str | None,
    significance_alpha: float | None,
) -> Any:
    if significance_source_column is None or significance_alpha is None:
        raise ValueError(
            "Significance configuration is required for significant delta outputs."
        )
    p_value = source_row[significance_source_column]
    if pd.notna(p_value) and p_value < significance_alpha:
        return source_row[value_col]
    return float("nan")


def _values_equal(left: Any, right: Any) -> bool:
    left_is_na = pd.isna(left)
    right_is_na = pd.isna(right)
    if bool(left_is_na) or bool(right_is_na):
        return bool(left_is_na) and bool(right_is_na)
    return bool(left == right)
