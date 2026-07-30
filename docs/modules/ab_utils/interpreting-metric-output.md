[AB utilities index](index.md)

# Interpreting Metric Output

[compute_test_metrics](functions/compute-test-metrics.md) returns comparison
rows that can be consumed directly or passed to
[format_ab_metrics](functions/format-ab-metrics.md). The first step is to read
which metric and comparison each row represents, then choose the uncertainty
columns that match the analysis mode.

- `metric_type` is `"mean"` for regular metrics and `"ratio"` for ratio metrics.
- `group_1` identifies the candidate side of the comparison and `group_2`
  identifies its baseline side. For test-versus-test rows, `group_2` is another
  test group rather than the experiment's configured control group.
- `metric_group_1` and `metric_group_2` contain the values for the corresponding
  group columns.
- `n_group_1` and `n_group_2` contain the corresponding sample sizes.
- `variance_group_1` and `variance_group_2` contain the variance inputs for the
  corresponding groups.
- `s.e.` is the standard error of `delta_abs`.
- `delta_abs` is `metric_group_1 - metric_group_2`; `delta_relative` and
  `mde_relative` use group 2 as the baseline denominator and are raw relative
  changes, e.g. `0.05` for 5%.
- `outliers_cutoff` contains the global metric cutoff used for the comparison.
- `outliers_n_group_1` and `outliers_n_group_2` count values or rows above the
  cutoff for the corresponding groups.
- CUPED columns such as `s.e. CUPED`, `p-value CUPED`, `mde_abs CUPED`, and
  `mde_relative CUPED` are added when pre-experiment data is supplied.
- Bootstrap columns such as `s.e. bootstrap` and `bootstrap_adj_p` are added
  when multiple-comparison adjustment is enabled.

## Group-position schema migration

The raw comparison schema uses group positions instead of control/test labels:

| Previous column | Current column |
| --- | --- |
| `n1` | `n_group_1` |
| `n0` | `n_group_2` |
| `outliers_n_test` | `outliers_n_group_1` |
| `outliers_n_control` | `outliers_n_group_2` |
| `metric_test` | `metric_group_1` |
| `metric_control` | `metric_group_2` |
| `variance_test` | `variance_group_1` |
| `variance_control` | `variance_group_2` |

`pre_exp_metrics_df` must contain the same group and user id columns used for
the main call, include the control label, and keep overlapping users in the same
groups. If a metric cannot be built from the pre-experiment dataframe, CUPED
statistics and CUPED MDE columns are set to `NaN` and a warning is emitted.

`bootstrap_adj_p` is a weak-null, max-T adjusted p-value computed separately for
each metric. Complete user rows are resampled with replacement inside their
observed groups, so group sizes and numerator/denominator pairing stay fixed.
Every replicate recomputes the pooled outlier cutoff and the full metric
estimator. Its centered statistic is
`(bootstrap delta - observed delta) / bootstrap standard error`, and the largest
absolute statistic across that metric's enabled, observed-valid comparisons is
used for adjustment.

A replicate contributes to `bootstrap_adj_p` only when every observed-valid
comparison in the metric family has a finite centered statistic. Discarded
replicates emit a warning; when no family replicate is valid, the adjusted
p-value is `NaN`. The calculation uses the finite-sample correction
`(1 + exceedances) / (1 + valid replicates)`, so a finite run never reports a
zero adjusted p-value. `s.e. bootstrap` is the sample standard deviation of the
finite bootstrap deltas for that row and does not require the whole family to be
valid. A fixed `bootstrap_random_state` produces the same replicates regardless
of worker count or process-to-thread fallback.

The procedure assumes independent one-row-per-user observations and stable
group assignments. As with other studentized bootstrap methods, very small or
degenerate groups can produce non-finite replicate standard errors and reduce
the effective resample count.

[format_ab_metrics](functions/format-ab-metrics.md) always adds a first
`group_size` row inside each `label_cols` group. Group-size counts must be
consistent for repeated groups inside the same `label_cols` group, including
groups listed in `allow_repeated_groups`.

[AB utilities index](index.md)
