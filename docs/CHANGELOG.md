[Documentation overview](README.md)

# Changelog

Generated from package version bumps and recent commit history.

## 1.3.7.6 - 2026-06-08

- Expanded the PyPI README with concise SQL transfer, AB metric, and date
  helper examples linked to module and function documentation.

## 1.3.7.5 - 2026-06-06

- Moved release validation Python logic into organized helper modules and kept
  only the pre-commit and full PyPI release scripts at the top level.

## 1.3.7.4 - 2026-06-06

- Added release checks for module function documentation coverage,
  documentation navigation links, and README dependency metadata.

## 1.3.7.3 - 2026-06-06

- Added release checks that keep the README package version synchronized with
  `pyproject.toml`.
- Added retry handling for PyPI and TestPyPI artifact verification installs.

## 1.3.6.18 - 2026-06-05

- Reworked non-SQL module documentation into SQL-style function references and
  workflow guides.
- Removed the legacy analytics toolkit manual in favor of module documentation.
- Updated the package summary wording for the PyPI README.

## 1.3.6.17 - 2026-06-05

- Renamed backend-only SQL inputs to backend-prefixed names and updated SQL
  function documentation grouping rules.

## 1.3.6.16 - 2026-06-05

- Added `sql.generate_dummy_connections()` for writing starter direct or
  Airflow-source `.connections` files without overwriting existing files.

## 1.3.6.15 - 2026-06-05

- Changed Excel `prettify=True` percentage and bounded decimal display formats
  to two decimal places.

## 1.3.6.14 - 2026-06-05

- Reworked the PyPI README into a CRAN-style package summary.
- Added a concise root README installation section.
- Moved detailed documentation into `docs/` and linked it from the package
  summary.
- Added PyPI/TestPyPI documentation rules for docs-only changes and dependency
  table updates.

## 1.3.6.12 - 2026-06-04

- Added MIT license metadata for PyPI packaging.
- Added a TestPyPI trusted-publishing path to the publish workflow.
- Documented the PyPI/TestPyPI release checklist and trusted publisher setup.

## 1.3.6.1 - 2026-06-03

- Added regression coverage that keeps new `time_print` keyword-only options
  optional for SQL timing wrappers and public dry-run paths.

## 1.3.6.0 - 2026-06-03

- Extended `time_print` with level filtering, structured context, stream
  routing, scoped context, and injectable clocks while keeping the timestamp-only
  output format.
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

[Documentation overview](README.md)
