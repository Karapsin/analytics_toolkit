[AB utilities index](index.md)

# Interpreting Metric Output

[compute_test_metrics](functions/compute-test-metrics.md) returns comparison
rows that can be consumed directly or passed to
[format_ab_metrics](functions/format-ab-metrics.md). The first step is to read
which metric and comparison each row represents, then choose the uncertainty
columns that match the analysis mode.

- `metric_type` is `"mean"` for regular metrics and `"ratio"` for ratio metrics.
- `group_1` and `group_2` are included when there are more than two experiment groups.
- `metric_control` and `metric_test` contain values in the baseline and test groups.
- `variance_control` and `variance_test` contain the variance inputs for each group.
- `s.e.` is the standard error of `delta_abs`.
- `delta_relative` and `mde_relative` are raw relative changes, e.g. `0.05` for 5%.
- `outliers_cutoff` contains the global metric cutoff used for the comparison.
- `outliers_n_control` and `outliers_n_test` count values or rows above the cutoff.
- CUPED columns such as `s.e. CUPED`, `p-value CUPED`, `mde_abs CUPED`, and
  `mde_relative CUPED` are added when pre-experiment data is supplied.
- Bootstrap columns such as `s.e. bootstrap` and `bootstrap_adj_p` are added
  when multiple-comparison adjustment is enabled.

`pre_exp_metrics_df` must contain the same group and user id columns used for
the main call, include the control label, and keep overlapping users in the same
groups. If a metric cannot be built from the pre-experiment dataframe, CUPED
statistics and CUPED MDE columns are set to `NaN` and a warning is emitted.

`bootstrap_adj_p` is computed per metric using a bootstrap max-statistic
procedure. Rows are resampled with replacement from the observed dataframe, each
sampled row keeps its original group label, and the maximum absolute comparison
statistic across enabled comparisons is collected. Treat it as a
bootstrap-adjusted stability measure rather than a strict null-calibrated
multiple-testing p-value.

[format_ab_metrics](functions/format-ab-metrics.md) always adds a first
`group_size` row inside each `label_cols` group. Group-size counts must be
consistent for repeated groups inside the same `label_cols` group, including
groups listed in `allow_repeated_groups`.

[AB utilities index](index.md)
