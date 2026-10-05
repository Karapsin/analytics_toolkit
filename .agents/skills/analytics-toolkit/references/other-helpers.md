# Other public helpers

Read the section matching the task and verify signatures in the target project's
installed version. These shortcuts are available after `from atk import *`.
These are common entrypoints, not an exhaustive API catalog.

## SQL

Use `sql` and the project's configured connection aliases. Public calls accept
alias keys such as `db_key`, `from_db`, and `to_db`; callers do not need to
construct backend clients for covered operations.

| Need | Public helper |
| --- | --- |
| Query result as dataframe, scalar, list, or dict | `sql.read` |
| DDL/DML execution | `sql.execute` |
| Setup SQL followed by a result query | `sql.execute_read` |
| Load a dataframe | `sql.load_df` |
| Create a table from a dataframe, schema, or query | `sql.create_table` |
| Insert query results into a table | `sql.insert`, `sql.execute_insert` |
| Setup SQL followed by table creation | `sql.execute_create` |
| Stream rows between configured database aliases | `sql.transfer` |

For an authorized read using an existing alias:

```python
from atk import *

frame = sql.read(db_key="warehouse", query="select 1 as value")
# frame is a dataframe with one column named value and one row containing 1
```

Use project connection setup and placeholder aliases in examples. Check output
shape and write-mode options rather than assuming a pandas SQL API is equivalent.
See the [SQL reference](https://github.com/Karapsin/analytics_toolkit/blob/dev/docs/modules/sql/functions/index.md).

## SQL formatting

Use `sql_format.format_sql` for deterministic formatting,
`sql_format.rewrite_with_ctes` for extracting derived SELECT subqueries, or
`sql_format.gp_rewrite_to_temp_tables` for Greenplum materialization rewrites.
See the [formatting reference](https://github.com/Karapsin/analytics_toolkit/blob/dev/docs/modules/sql_format/functions/index.md).

## AB analysis

Prefer `ab.compute_test_metrics` for experiment comparisons,
`ab.format_ab_metrics` for report presentation, `ab.do_split` for group assignment,
and `ab.compute_mde` for planning. SQL-backed metric and MDE entrypoints are also
available; check the installed public exports when the task needs SQL-side
computation or segmented reports.

```python
from atk import *

metrics = ab.compute_test_metrics(
    df=user_metrics,
    group="group",
    control="control",
    user_id="user_id",
    outliers_quantile=1,
)
# metrics is a dataframe of experiment comparisons
```

Here `user_metrics` has one row per user, a unique non-null `user_id`, a non-null
`group`, and numeric metric columns. Ratio metrics need explicit specifications.
Select outlier treatment, comparison groups, and bootstrap settings for the
analysis; the example leaves the maximum value unmodified. See the
[AB reference](https://github.com/Karapsin/analytics_toolkit/blob/dev/docs/modules/ab_utils/functions/index.md).

## Excel

Choose `excel.pivot_and_break_table` for pivoted reports or `excel.break_table`
when the dataframe already has the desired table shape. Both support grouping
into sheets and table blocks, including multiple dataframes placed side by side.

```python
from atk import *

tables = excel.break_table(
    df=report_df,
    output="report.xlsx",
    break_by="segment",
    sheet_by="report_date",
)
# writes report.xlsx and returns a mapping from sheet values to table lists
```

Here `report_df` includes `segment` and `report_date` columns. For a plain
dataframe export without the toolkit's report behavior, an existing pandas export
can still fit the task. See the
[Excel reference](https://github.com/Karapsin/analytics_toolkit/blob/dev/docs/modules/excel/functions/index.md).

## Files and logging

Use the general aliases exported by `from atk import *`:

- `here(filename)` resolves a file relative to the active script/editor context.
- `from_here(filename, levels_up=...)` resolves a path above that context.
- `read_file_here` reads a relative file and supports `str.format` parameters via
  `params_dict`. For explicit-path reads, add
  `from analytics_toolkit.general import read_file`; `read_file` is not an `atk`
  wildcard export.
- `write_file` writes text; `time_print` emits timestamped messages, and
  `get_time_print_sink` returns the active logging sink. Additional logging
  configuration and context helpers need explicit public imports from
  `analytics_toolkit.general`.

```python
from atk import *

query = read_file_here("queries/report.sql")
time_print("Loaded report query")
# query contains the SQL file's text; the message includes a timestamp
```

Verify path resolution against the task's execution context. Use ordinary path
or logging APIs when requirements extend beyond these helper contracts.
