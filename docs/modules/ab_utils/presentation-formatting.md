[AB utilities index](index.md)

# Presentation Formatting

Metric comparison output is row-oriented. Use
[format_ab_metrics](functions/format-ab-metrics.md) when a report or Excel
workflow needs a wide presentation table with one column family per experiment
group or comparison.

```python
from analytics_toolkit.ab_utils import format_ab_metrics

formatted = format_ab_metrics(
    result["segment_1"],
    label_cols=["segment"],
    output_type=["metric_values", "p_values", "delta_relative_significant"],
    significance_alpha=0.05,
    significance_p_value="p_values",
    allow_repeated_groups=["control"],
)
```

With the default output selection, the formatted table contains label columns,
`metric`, and one metric-value column per experiment group. Additional output
types add comparison columns such as p-values, absolute deltas, relative deltas,
and MDE values.

Significant delta outputs keep the delta only when the configured p-value is
below `significance_alpha`; otherwise they return `NaN`. Choose the p-value
source with `significance_p_value`, for example `p_values`, `p_values_cuped`, or
`p_values_adj`.

Use `allow_repeated_groups` when a shared group such as `control` appears in
multiple comparisons for the same metric. Use `keep_simple_group_names=True`
with a single comparison output when comparison columns should be named by test
group only, for example `test_1` instead of
`test_1_vs_control_delta_relative`.

[AB utilities index](index.md)
