# SQL assets

`ql_*.sql` are editable chart queries. `projections/*.sql` are explicit dataset
source projections. Both resolve `__TABLE_RETAIL__`, `__TABLE_ACQUISITION__`, and
`__TABLE_DELIVERY__` from the dataset table settings before SDK publication.
The acquisition role uses the physical customers table.

Keep table tokens separate from QL parameter placeholders such as `{{region}}`
and `{{interval_from}}`. DataLens supplies those parameter values; Python must
not interpolate them into SQL. Every query explicitly declares its result
aliases and casts in its chart JSON. The version-matched SDK prescribes the typed `integer` QL cast for numeric
results, including floats. Do not truncate SQL results to match that metadata.
The deployed renderer still requires fractional-value verification.

Write multivalue predicates as `region IN {{region}}`, without parentheses
around the placeholder. The official QL formatter supplies the tuple for an
`IN` operation; adding parentheses produces nested tuples when several values
are selected. The same rule applies to channel selectors. See the
[pinned official formatter](https://github.com/datalens-tech/datalens-ui/blob/f581b7c31d6e9189ebeb1e1632b5fe7570534fb8/src/server/modes/charts/plugins/ql/utils/misc-helpers.ts).
The manual "Priority only" checkbox is optional: false includes all rows,
and true selects priority rows.

The source projections preserve the native ClickHouse type in catalogue
companion fields. Types that DataLens cannot represent directly have explicit
string or scalar projections. Exact text is retained for wide integers and
precision-sensitive decimals. These examples are synthetic; production
projections should select the useful fields rather than reproduce the entire
type catalogue.

Business numeric source fields are dimensions named `Revenue Raw`, `Orders
Raw`, and similar. Dataset formulas such as `SUM([Revenue Raw])` provide their
public measures. This prevents an accidental second aggregation and keeps raw
inputs available for conditional, level-of-detail, and window formulas.

`ql_flat_table.sql` demonstrates null-safe ratios and conditional aggregation.
Acquisition chart queries select stage 4 (Purchase) when reporting acquired
customers; stage-level counts must not be summed across the funnel.

`Compound Score` shows `SUM(Revenue) + SUM(Profit) * (SUM(Orders))`. It is an
educational precedence example and must not be treated as a production KPI.

The QL detail table renders monetary values and AOV as text rounded to two decimal places,
and Margin as a ratio rounded to four decimal places. Its query sorts by numeric revenue
before formatting. Chart axes retain the SDK generic numeric QL cast.

The [official open-source QL numeric parser](https://github.com/datalens-tech/datalens-ui/blob/f581b7c31d6e9189ebeb1e1632b5fe7570534fb8/src/server/modes/charts/plugins/ql/utils/value-helpers.ts)
uses `Number(value)` for both plotted and table numeric values. This preserves
fractional input values in that source version; the running DataLens deployment
must be checked separately.
