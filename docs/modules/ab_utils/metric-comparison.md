[AB utilities index](index.md)

# Metric Comparison Workflow

Metric comparison starts from one row per user. The dataframe needs a group
column, a unique user id column, and metric columns that can be interpreted as
numeric means or ratio components. Use
[compute_test_metrics](functions/compute-test-metrics.md) for the comparison
step and keep exact option defaults in the function reference.

```python
from analytics_toolkit.ab_utils import compute_test_metrics

metrics = compute_test_metrics(
    df=experiment_df,
    group="group_name",
    control="control",
    user_id="user_id",
    ratio_metrics=[
        {"name": "ctr", "numerator": "clicks", "denominator": "views"},
    ],
    test_vs_test=False,
)
```

Mean metrics use their non-missing metric values. Ratio metrics are configured
separately because they need numerator and denominator columns; see
[Ratio Metrics](ratio-metrics.md) for aggregate and per-user ratio behavior.

Outlier handling is enabled by default. Each metric gets one upper-tail cutoff
across all experiment groups. The default `"non_zero_truncate"` policy computes
that cutoff from non-zero values, then caps values above the cutoff while keeping
zeros in the metric sample. `"truncate"` computes the cutoff from all non-missing
values and caps values above it; `"drop"` treats values above the cutoff as
missing. Aggregate ratio outliers are handled through numerator and denominator
sums so the final ratio remains an aggregate estimate.

The reported `mde_abs` and `mde_relative` use a normal approximation based on
observed group variances and sample sizes. When CUPED input is provided, CUPED
standard errors, p-values, and MDE columns are added without aborting the whole
metric computation when a single CUPED metric cannot be built. Bootstrap
multiple-comparison adjustment adds weak-null max-T `bootstrap_adj_p` and
`s.e. bootstrap` outputs when enabled. It resamples complete user rows within
their observed groups, recomputes pooled outlier handling in each replicate,
and adjusts across all observed-valid comparisons for each metric.

Use [Interpreting Metric Output](interpreting-metric-output.md) for the meaning
of the output columns and [Presentation Formatting](presentation-formatting.md)
when the result should be reshaped into report tables.

[AB utilities index](index.md)
