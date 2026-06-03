# Changelog

Generated from package version bumps and recent commit history.

## 1.3.6.1 - 2026-06-03

- Added regression coverage that keeps new `time_print` keyword-only options
  optional for SQL timing wrappers and public dry-run paths.

## 1.3.6.0 - 2026-06-03

- Extended `time_print` with level filtering, structured context, stream
  routing, scoped context, and injectable clocks while preserving legacy output.
- Added public `time_print` configuration helpers through
  `analytics_toolkit.general`.
- Added structured context to central SQL operation/timing logs and warning
  levels for retry failure logs.

## 1.3.5.0 - 2026-06-03

- Added `compute_mde_only` for pre-test AB planning from historical variance.
- Added public AB option bundles `RatioMetricSpec` and `MdePlanningOptions`.
- Added quarter support to date helpers, including `add_quarters` and quarterly
  `gen_dates_list` sequences.
- Exported public SQL type aliases for common connection, backend, table, and
  task annotations.
- Added cross-module SQL to AB metrics to Excel report example.
- Added regression coverage for date sequence properties, formatter row
  ordering, SQL identifier quoting, SQL dry-run option coverage, and AB planning.

## Recent Pre-Changelog History

- `1.3.4.0`: added a guaranteed `group_size` row to AB metric formatting.
- `1.3.3.0`: added CUPED MDE outputs to AB metrics.
- `1.3.2.x`: treated deterministic ClickHouse and SQL semantic errors as
  non-retryable.
- `1.3.1.x`: added `sanitize_date` and strengthened CUPED tests.
