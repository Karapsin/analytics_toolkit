[AB utilities index](../index.md)

# All AB Functions

Use `from analytics_toolkit.ab_utils import ...` in user-facing code. General
functions are listed before advanced batch helpers. Within each section, the
helpers most likely to be used in normal workflows appear first.

## General Functions

- [compute_test_metrics](compute-test-metrics.md) - Compare experiment groups across metrics.
- [format_ab_metrics](format-ab-metrics.md) - Reshape metric output for reports.
- [do_split](do-split.md) - Assign users to AB groups.
- [compute_mde_only](compute-mde-only.md) - Estimate pre-test MDE.

## Advanced Functions

- [parallel_compute_metrics](parallel-compute-metrics.md) - Run metric tasks concurrently.
- [parallel_compute_metrics_from_sql](parallel-compute-metrics-from-sql.md) - Load SQL tasks and compute metrics.

[AB utilities index](../index.md)
