[AB utilities index](index.md)

# Compute Test Metrics

`compute_test_metrics` expects:

- one row per user
- a group-label column
- a unique user id column
- all remaining columns to be numeric metrics

Missing metric values are ignored on a per-metric basis.

Outlier handling is enabled by default. For each metric, an upper-tail cutoff is
computed once across all experiment groups from `outliers_quantile=0.999`.
Values above the cutoff are handled according to `outliers_policy`:

- `"truncate"` (default): cap values at the cutoff
- `"drop"`: treat values above the cutoff as missing

Mean metrics use their non-missing metric values for the cutoff. `level="user"`
ratio metrics use valid per-user ratios after denominator filtering. `level="agg"`
ratio metrics identify outlier rows from `numerator / denominator` only when the
row denominator is positive; denominator values `<= 0` keep the existing aggregate
ratio behavior and are not classified as row-ratio outliers. For aggregate ratios,
`"drop"` excludes outlier rows from numerator/denominator sums and variance, while
`"truncate"` replaces an outlier row numerator with `cutoff * denominator`.

The reported `mde_abs` and `mde_relative` use a normal approximation based on the
observed group variances and sample sizes. When CUPED is enabled, `mde_abs CUPED`
and `mde_relative CUPED` use the same approximation with the CUPED-adjusted
standard error.

Other function options:

- `mde_alpha=0.05`
- `mde_power=0.80`
- `outliers_quantile=0.999`: upper-tail quantile used for the per-metric outlier cutoff
- `outliers_policy="truncate"`: either `"truncate"` or `"drop"`
- `pre_exp_metrics_df=None`: optional pre-experiment dataframe used to compute CUPED-adjusted standard errors, p-values, and MDE
- `test_vs_test=True`: when `False`, only compare each test group against control
- `multiple_comparisons_adjustment=False`: when `True`, add `s.e. bootstrap` and `bootstrap_adj_p`
- `multiple_comparisons_adjustment_resamples=2000`: number of bootstrap resamples for `s.e. bootstrap` and `bootstrap_adj_p`
- `bootstrap_random_state=0`: bootstrap RNG seed; set `None` for non-deterministic resampling
- `bootstrap_n_jobs=1`: number of worker executors for bootstrap batches
- `bootstrap_progress=False`: when `True`, show a `tqdm` progress bar for bootstrap resamples

[AB utilities index](index.md)
