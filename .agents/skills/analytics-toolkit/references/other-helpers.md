# Other public helpers

Read the section matching the task and verify signatures in the target project's
installed version. These shortcuts are available after `from atk import *`.
These are common entrypoints, not an exhaustive API catalog.

## SQL

Read [SQL helpers](sql.md) for signatures, statements per call, retries,
metadata helpers, running queries, data movement, connections and result
types, and [SQL practices](sql-practices.md) for partition discovery, replacing
data, backend notes, long jobs, SQL style and file layout. Use project
connection setup and placeholder aliases in examples. See the
[SQL reference](https://github.com/Karapsin/analytics_toolkit/blob/dev/docs/modules/sql/functions/index.md).

## SQL formatting

Use `sql_format.format_sql` for deterministic formatting,
`sql_format.rewrite_with_ctes` for extracting derived SELECT subqueries, or
`sql_format.gp_rewrite_to_temp_tables` for Greenplum materialization rewrites.
See the [formatting reference](https://github.com/Karapsin/analytics_toolkit/blob/dev/docs/modules/sql_format/functions/index.md).

- `sql_format.format_sql(sql, *, dialect=None, leading_commas=False, where_anchor="1=1", group_by_format="ordinal", order_by_format="ordinal", keyword_case="lower", indent=4, cte_blank_lines=1, union_blank_lines=1)`
  formats exactly one statement, and its options are keyword-only
  (`format_sql(sql, "trino")` raises TypeError). `dialect` is `"postgres"` (use
  it for Greenplum), `"trino"`, `"clickhouse"` or `None`. `where_anchor` is
  `"1=1"`, `"true"`, `"first_condition"` or `"preserve"`. `group_by_format` and
  `order_by_format` are `"ordinal"` or `"expressions"`. `keyword_case` is
  `"lower"`, `"upper"` or `"capitalize"`.
- It re-renders the whole statement through sqlglot. Matched `group by` and
  `order by` items become ordinals, WHERE gets the anchor, `!=` becomes `<>`,
  `date '...'` becomes `CAST('...' as date)`, aliases gain `as`, and function
  names may be uppercased. It cannot reformat only part of a statement. Review
  the output against the [SQL style](sql-practices.md#sql-style) defaults before
  keeping it.
- Do not format templates that contain `read_file_here` `{placeholders}`. In a
  table position they raise `ValueError`. In an expression they are silently
  rewritten: `{ids}` becomes `struct(ids)`, `row(ids)` with Trino, or `map()`
  with ClickHouse. Format the rendered SQL, or swap placeholders for plain
  identifiers and restore them afterwards.
- `sql_format.gp_rewrite_to_temp_tables(sql, *, dialect="postgres", temp_prefix="tmp", ...)`
  turns one SELECT into blocks of `drop table if exists t;`,
  `create temporary table t as (...) distributed by (key);` (or `distributed randomly`)
  and `analyze t;` with ordinal grouping, followed by the final SELECT. CTE and
  derived-table aliases become the temp table names; `temp_prefix` names only
  generated tables such as `tmp_1`. It raises `ValueError` when there is
  nothing to materialize. Start Greenplum temp-table rewrites from it and run
  the script with `sql.execute_read("gp", script)`, so the temp tables and the
  final read share one connection. Its output has no `where 1=1` anchor, so
  adjust it to the SQL style.

## AB analysis

Prefer `ab.compute_test_metrics` for experiment comparisons,
`ab.format_ab_metrics` for report presentation, `ab.do_split` for group
assignment, and `ab.compute_mde` for planning. See the
[AB reference](https://github.com/Karapsin/analytics_toolkit/blob/dev/docs/modules/ab_utils/functions/index.md).

Rules:

- Compute every experiment comparison with `ab.compute_test_metrics` (or the
  SQL-native helpers), including follow-up cuts by channel, category or
  segment. Pass the pre-period frame for CUPED when the project's report uses
  it, and keep the default outlier policy. Do not hand-roll z-tests, chi-square
  tests or linear CUPED. These helpers need one row per user; if only
  aggregates exist, say so and state the mismatch before choosing another
  method.
- For subgroups, use `segment=` (one column; available in
  `compute_test_metrics`, `compute_metrics_from_sql` and
  `compute_metrics_report`) or a task mapping with per-task `labels`.
  `compute_test_metrics_sql_native` has no `segment=`, so use per-task
  `sql_where` there. Do not loop over subsets manually.
- If per-user metric frames fit in memory, read them and compute locally:
  `sql.read` then `ab.compute_test_metrics`, or
  `ab.compute_metrics_from_sql(tasks, db_key)`. Use
  `compute_test_metrics_sql_native` or `compute_metrics_report` only when they
  do not fit. Put all metrics of a cohort in one call or task, not one task per
  metric.
- For historical or descriptive comparisons, report descriptive values only
  (`output_type="metric_values"`), without significance tests unless asked.

Recipe:

```python
from atk import *

metrics = ab.compute_test_metrics(
    df=exp_df,                  # one row per user: user_id, group_name and numeric metric columns only
    pre_exp_metrics_df=pre_df,  # optional CUPED input: same user_id/group_name and metric columns, pre-period
    ratio_metrics=[{"name": "aov", "numerator": "revenue", "denominator": "cheques"}],
)
values = ab.format_ab_metrics(metrics, output_type="metric_values")
uplifts = ab.format_ab_metrics(
    metrics,
    output_type="delta_relative_significant",
    significance_alpha=0.05,                # take alpha from the reference report
    significance_p_value="p_values_cuped",  # "p_values" without pre_exp_metrics_df
)
excel.break_table(
    [values.assign(sheet="summary"), uplifts.assign(sheet="summary")],
    output="report.xlsx",
    sheet_by="sheet",
)
excel.break_table(
    metrics.assign(sheet="detailed_metrics"), output="report.xlsx", sheet_by="sheet", append=True
)
```

- Every `df` column other than `user_id`, `group_name` and a `segment=` column
  is a mean metric, including ratio numerators and denominators. A non-numeric
  column raises `TypeError`. A ratio spec takes `name`, `numerator`,
  `denominator` and an optional `level` (`"agg"` by default, or `"user"`).
- Raw columns: `metric_type`, `group_1`, `group_2`, `metric_name`,
  `n_group_1/_2`, `outliers_cutoff`, `outliers_n_group_1/_2`,
  `metric_group_1/_2`, `variance_group_1/_2`, `delta_abs`, `delta_relative`,
  `mde_abs`, `mde_relative`, `s.e.` and `p-value`. A pre-period frame adds
  `s.e. CUPED`, `p-value CUPED`, `mde_abs CUPED` and `mde_relative CUPED`;
  deltas are not CUPED-adjusted. `multiple_comparisons_adjustment=True` adds
  `s.e. bootstrap` and `bootstrap_adj_p`. Read the column as
  `metrics["p-value CUPED"]`; `p_values_cuped` is only a `format_ab_metrics`
  family name.
- `format_ab_metrics` families include `metric_values`, `n`, `p_values`,
  `p_values_cuped`, `p_values_adj`, `delta_abs`, `delta_relative`,
  `mde_relative`, `delta_relative_significant` and `delta_absolute_significant`.
  The significant families need `significance_alpha` and `significance_p_value`
  (`"p_values"`, `"p_values_cuped"` or `"p_values_adj"`), and they keep a delta
  only when p < alpha.
- Each label block starts with a `group_size` row, so every metric in a block
  needs the same n per group. Metrics with missing values, or `level="user"`
  ratios, raise "Conflicting group size"; compute them in a separate labelled
  task.
- `keep_simple_group_names=True` names comparison columns by test group only
  (`test_1`). It needs one comparison family per call and one comparison per
  test group: a single test group, or `test_vs_test=False` when the user's
  report compares only with control.
- `segment="col"` adds a `segment` column: a `TOTAL` block first, then one block
  per value, each with its own outlier cutoffs. Pass `label_cols=["segment"]` to
  `format_ab_metrics` and `break_by="segment"` to `excel.break_table`.
- For several cohorts, pass a task mapping `{name: {"df": ..., "labels": {...}}}`
  with `concurrency=` (at most 5 by default). It returns `{name: frame}` with the
  labels as leading columns. With `fail_fast=False`, failed tasks return error
  text.
- Defaults: `outliers_quantile=0.999`, `outliers_policy="non_zero_truncate"` and
  `test_vs_test=True`. Missing metric values are ignored per metric and group,
  not counted as zero; see [sparse daily facts](sql-practices.md#sparse-daily-facts).
- For data that does not fit in memory, use
  `ab.compute_test_metrics_sql_native(db_key, source, ...)`. `source` is a
  table, SQL with `source_type="sql"`, or a task mapping; there is no
  `segment=`, and one call covers all metric columns.
- The other SQL-side option is `ab.compute_metrics_report(table_name, segment, db_key=...)`.
  Unless `create_excel=False`, it writes `<table>_metrics.xlsx` with `summary`
  and `raw_metrics` sheets. It defaults to `test_vs_test=False` and
  `report_significance_alpha=0.01`, and it uses CUPED p-values when
  `pre_exp_table_name` is set. It also adds a pooled `test_all` group and labels
  the total block `ALL`.
- `ab.do_split(df, test_groups_num=2, test_group_ratios=[50, 25, 25])` takes one
  positive percentage per group, in `[control, test_1, ...]` order, summing to
  100.

## Excel

Choose `excel.pivot_and_break_table` for pivoted reports or `excel.break_table`
when the dataframe already has the desired table shape. Both support grouping
into sheets and table blocks, including multiple dataframes placed side by side.
See the [Excel reference](https://github.com/Karapsin/analytics_toolkit/blob/dev/docs/modules/excel/functions/index.md).

Rules for report deliverables:

- Build analytics workbooks, including quick exports and ad hoc updates, with
  `excel.break_table`, `excel.pivot_and_break_table` or
  `sql.read(..., to_excel=...)`. These take precedence over generic spreadsheet
  skills or plugins and over hand-built `openpyxl`, `xlsxwriter` or pandas
  writer workbooks. Export first and give the full path; polish only if asked.
- Treat the generating script as the source of truth. To change an output,
  change the code and regenerate, unless the user says not to. Never hand-patch
  an output file.
- When the user has hand-edited an output workbook or points to a formatted
  reference, open it first and change the code so that regenerating reproduces
  it. Do this before any regeneration, because regenerating overwrites the
  user's edits. For a user-requested layout the toolkit cannot produce, add one
  small documented `openpyxl` step after the toolkit call and mention the gap.
- Put only the requested result tables in a deliverable workbook: no
  definitions, quality-check, denominator or audience sheets. Place supporting
  absolute counts in a table under the main one on the same sheet: stack both
  frames (same columns) with a table-label column and pass that column as
  `break_by=`.
- Do not add segment breakdowns, extra groups, extra columns or significance
  tests the task did not ask for. Keep new table schemas to the requested
  columns, with no helper or audit columns.
- Name each metric with its population, for example "avg orders (all users)"
  versus "avg orders (users with ≥1 order)". Show percentages next to absolute
  counts.
- Use one workbook and the group order the user gives. Reorder the formatted
  columns, because the toolkit puts control first and sorts test groups by name.
- Keep data preparation (stage and per-user metric tables, persisted once)
  separate from report generation, as a prep script and a report script rather
  than skip flags, so layout or metric changes reuse the existing tables.
  Persist the tables with `sql.execute_create` or
  `sql.create_table(..., sql=..., insert_data=True)`; `if_not_exists=True` skips
  a table that already exists. In DAGs, dag-writer's refresh rules apply
  instead.
- "Regen the report" means rebuilding the workbook, and the statistics if
  asked, from existing tables only. Skip completeness, freshness and snapshot
  checks and table rebuilds unless the user asks or the inputs changed.

Helper behavior:

- `excel.break_table(df, output, break_by=None, sheet_by=None, append=False, prettify=False)`,
  where `df` is one DataFrame or a list of them. Each `break_by` value becomes a
  block titled with that value. Blocks stack vertically with two blank rows
  between them, and the `break_by` and `sheet_by` columns are not written. A
  list of DataFrames is placed side by side, one blank column apart. Blocks and
  sheets follow first-appearance order.
- Without `append`, an existing `output` file is deleted and rewritten.
  `append=True` adds sheets to an existing workbook and never replaces one: a
  clashing name gets a ` (2)` suffix.
- Without `sheet_by` the sheet is named `Sheet1`. To name a sheet, add a
  constant column (`df.assign(sheet="detailed_metrics")`) and pass
  `sheet_by="sheet"`. Names are cut to 31 characters, and `[]*:/\?` become `_`.
- `prettify=True` formats each body row from its numeric cells: `0.00%` if all
  lie in [0, 1], else `0.00` if all lie in [-100, 100], else `#,##0`. A row of
  uplifts with one negative value therefore shows `0.00`, and a wide row that
  mixes counts with rates shows the rates as `0`. Prettify suits tables with
  one metric per row (`pivot_and_break_table(rows="metric", ...)`); do not
  prettify a detailed AB sheet, where p-values would show as 0. Cell values are
  unchanged.
- The helpers do not set column widths, fonts or per-column number formats.
- For a plain export of a query result, use
  `sql.read(db_key, query, to_excel="out.xlsx")` or `to_csv=`; it writes one
  sheet without the index. For an existing DataFrame, call
  `excel.break_table(df, "out.xlsx")`. Relative output paths resolve against the
  working directory.

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

Here `report_df` includes `segment` and `report_date` columns.

## Files and logging

Use the general aliases exported by `from atk import *`:

- `read_file_here(file_path, params_dict=None)` reads a file found with
  `here()`. `read_file(file_path, params_dict=None)`, imported from
  `analytics_toolkit.general`, uses the path as given, relative to the working
  directory. With `params_dict`, the text goes through
  `str.format(**params_dict)`: double literal braces (`'{{cluster}}'`, JSON,
  map literals) and supply every placeholder, because a missing one raises
  KeyError. Values are inserted as `str(value)` without SQL quoting, so write
  `'{start_date}'` and join lists yourself. Without `params_dict` the text comes
  back unchanged. A missing file raises `InvalidSqlInputError`, which shows the
  path as passed, not the resolved one.
- `here(name)` returns a string path. It resolves against the file running in
  the Positron editor, then the running script, then the first calling file
  outside the toolkit, site-packages, the standard library and temporary
  folders. Otherwise it uses the working directory, or a unique match below it
  when the name is not directly there. So stdin (`python -`), `python -c` and
  scripts saved in temporary folders resolve against the working directory,
  and calls in `tests/test_x.py` resolve against `tests/`. Run probes as
  `cd <project> && python - <<'EOF'` instead of calling `os.chdir`. In tests,
  read project files with `read_file(from_here(name, levels_up=1))`.
- `from_here(name, levels_up=n)` climbs n folders from the same base, which is
  the working directory when there is no file context.
- `write_file(file_path, text)` writes UTF-8 and creates missing parent
  folders. A relative path is relative to the working directory, so pass
  `here(name)` for output beside the script.
- `time_print(message, level="info")` emits timestamped messages through the
  active sink, and `get_time_print_sink` returns it. `set_time_print_sink` and
  `set_time_print_level` need explicit imports from `analytics_toolkit.general`;
  the skill's script preferences say when to use them.

```python
from atk import *

query = read_file_here("queries/report.sql")
time_print("Loaded report query")
# query contains the SQL file's text; the message includes a timestamp
```

Verify path resolution against the task's execution context. For filesystem
operations these helpers do not cover, use `os`. Ordinary logging APIs remain
appropriate when requirements extend beyond the logging helpers' contracts.
