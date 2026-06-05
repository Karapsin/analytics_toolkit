[AB utilities index](index.md)

# Format AB Metrics

Format metric comparison output for presentation with `format_ab_metrics`:

```python
formatted = format_ab_metrics(
    result["segment_1"],
    label_cols=["segment"],
    output_type=["metric_values", "p_values", "delta_relative_significant"],
    significance_alpha=0.05,
    significance_p_value="p_values",
    allow_repeated_groups=["control"],
)
```

With the default `output_type`, the result is a wide table with label columns,
`metric`, and one metric-value column per experiment group. Additional output
types add comparison columns such as `test_vs_control_p_value` and
`test_vs_control_delta_relative`. `output_type` accepts either one output name
or a list of output names. CUPED MDE can be selected with `mde_abs_cuped` and
`mde_relative_cuped`. Significant delta outputs add columns such as
`test_vs_control_delta_relative_significant` and keep the delta only when the
configured p-value is below `significance_alpha`; otherwise they return `NaN`.
Use `significance_p_value="p_values"`, `"p_values_cuped"`, or `"p_values_adj"`
to choose the p-value source. Use `allow_repeated_groups` when a shared group
such as `"control"` appears in multiple comparisons for the same metric and the
formatted table should keep the first value for that repeated group.
Use `keep_simple_group_names=True` with a single comparison output such as
`output_type=["delta_relative"]` to name comparison columns by test group only,
for example `test_1` instead of `test_1_vs_control_delta_relative`.

```python
formatted_delta = format_ab_metrics(
    result["segment_1"],
    label_cols=["segment"],
    output_type=["delta_relative"],
    keep_simple_group_names=True,
)
```

[AB utilities index](index.md)
