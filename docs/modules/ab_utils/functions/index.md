[AB utilities index](../index.md)

# All AB Functions

Use `from analytics_toolkit.ab_utils import ...` in user-facing code. General
functions are listed before advanced batch helpers. Within each section, the
helpers most likely to be used in normal workflows appear first.

## General Functions

- [compute_test_metrics](compute-test-metrics.md) - compare experiment groups across metrics
- [compute_test_metrics_sql_native](compute-test-metrics-sql-native.md) - compare experiment groups with SQL-side stats
- [format_ab_metrics](format-ab-metrics.md) - reshape metric output for reports
- [do_split](do-split.md) - assign users to AB groups
- [compute_mde](compute-mde.md) - estimate MDE planning scenarios
- [compute_mde_from_sql](compute-mde-from-sql.md) - estimate MDE planning scenarios from a SQL table
- [compute_mde_sql_native](compute-mde-sql-native.md) - estimate MDE scenarios with SQL-side stats
- [compute_metrics_from_sql](compute-metrics-from-sql.md) - load SQL tasks and compute metrics

## Advanced Functions

- [compute_test_metrics](compute-test-metrics.md) task-map mode - run dataframe metric tasks concurrently

[AB utilities index](../index.md)
