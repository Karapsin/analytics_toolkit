# AB Utilities Agent Instructions

Read this file for AB utilities code, tests, docs, API explanation, or behavior
investigation.

## AB Utilities Contracts

- `compute_test_metrics` expects one row per user, a non-null unique user id, a non-null group column, and at least one mean or ratio metric.
- Output column order is part of the API; preserve placement of `metric_type`, group columns, `p-value CUPED`, and `bootstrap_adj_p`.
- `analytics_toolkit.ab_utils.metrics` re-exports many underscore helpers. Tests may import those names directly.
- Ratio metrics support only `level="agg"` or `level="user"` and `invalid_denominator="ignore"`.
- Missing metric values are ignored per metric/group; non-numeric metric values should raise.
- CUPED failures should warn and return `NaN`, not abort the whole metric computation when validation has passed.
- Bootstrap multiple-comparison adjustment should remain deterministic when `bootstrap_random_state` is set and should fall back from process pools to threads when process pools are unavailable.
