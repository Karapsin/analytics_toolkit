[AB utilities index](index.md)

# Output Notes

- `format_ab_metrics` always adds a first `group_size` row inside each `label_cols`
  group; sizes come from `n0` for `group_2` and `n1` for `group_1`
- group-size counts must be consistent for repeated groups inside the same
  `label_cols` group, including groups listed in `allow_repeated_groups`
- ratio metric names use the provided ratio metric `name` directly
- `metric_type` is `"mean"` for regular metrics and `"ratio"` for ratio metrics
- `group_1` and `group_2` are included when there are more than two experiment groups
- `metric_control` and `metric_test` contain the metric value in the baseline and test groups
- `outliers_cutoff` contains the global metric cutoff used for the comparison
- `outliers_n_control` and `outliers_n_test` count values or rows above the cutoff in the baseline and test groups
- `variance_control` and `variance_test` contain the uncertainty variance inputs for each group
- `s.e.` is the standard error of `delta_abs`
- `delta_relative` and `mde_relative` are raw relative changes, e.g. `0.05` for 5%
- `delta_relative_significant` and `delta_absolute_significant` format `delta_relative`
  and `delta_abs` only when the configured p-value is significant
- when `pre_exp_metrics_df` is provided, `s.e. CUPED`, `p-value CUPED`,
  `mde_abs CUPED`, and `mde_relative CUPED` are added after `p-value`
- when `multiple_comparisons_adjustment=True`, `s.e. bootstrap` and `bootstrap_adj_p` are added after CUPED columns, if any

`pre_exp_metrics_df` requirements:

- it must contain the same `group` and `user_id` columns used for the main call
- it must contain the control label in the same group column
- overlapping `user_id` values must map to the same experiment group in both dataframes
- if a metric cannot be built from the pre-experiment dataframe, CUPED statistics
  and CUPED MDE columns are set to `NaN` and a warning is emitted

`bootstrap_adj_p` is computed per metric using a bootstrap max-statistic procedure:

- rows are resampled with replacement from the observed dataframe
- each sampled row keeps its original group label
- for each metric, the maximum absolute comparison statistic across enabled comparisons is collected
- `bootstrap_adj_p` is the share of bootstrap max-statistics at least as large as the observed absolute statistic
- `s.e. bootstrap` is the sample standard deviation of bootstrapped `delta_abs` estimates for the same metric/comparison

This is a bootstrap-based empirical adjustment on the observed grouped data. It should
be interpreted as a heuristic bootstrap-adjusted significance/stability measure rather
than a strict null-calibrated multiple-testing p-value.

[AB utilities index](index.md)
