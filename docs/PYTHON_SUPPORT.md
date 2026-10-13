[Documentation overview](README.md)

# Python support

The normal support window contains the seven latest stable Python minor
versions. Prereleases do not advance the window. A new minor is added only
after core installation and tests pass.

When any extra is unavailable on the newest minor, the oldest supported minor
is retained. Further releases may extend the window to eight, nine, or more
minors. Once every extra and the combined `all` installation pass on the newest
minor, support shrinks back to the latest seven. Python 3.8 was removed as a
one-time exception; it will not be reintroduced.

## Accepted compatibility ranges

<!-- support-table -->
| Installation | Supported Python minors |
| --- | --- |
| Core | 3.9-3.15 |
| `airflow` | 3.9-3.14 |
| `clickhouse-native` | 3.9-3.14 |
| `tui` | 3.9-3.14 |
| `datalens` | 3.10-3.14 |
| `all` | 3.10-3.14 |
<!-- /support-table -->

These ranges describe validated installations, not just dependency markers.
`all` is the intersection of core and every individual extra's range. Install
individual extras when the combined installation does not support your Python.
An omitted dependency does not count as a supported extra.

Airflow 2 and 3 are supported through the same SQL APIs. Python 3.9 can resolve
an older compatible Airflow release; newer Python versions may require Airflow
3. Connections and Variables are looked up lazily using the installed major's
API. Airflow 3 uses its public Task SDK during task execution. SQL facade imports
do not contact Airflow or open connections during DAG parsing.

The table records accepted core support. Extras retain their validated ranges
while newest-minor probes establish readiness. Adding Airflow 3 does not imply
Airflow supports the newest Python minor.
On a newly released minor, native dependencies may build from source; installation
requires a working C/C++ toolchain where wheels are unavailable. Compatible pandas
2 releases remain available on older minors; newer minors can resolve pandas 3.

SQLGlot 30.22 is the minimum supported release. The older 26.33 parser exceeded
Python 3.9’s recursion limit on deeply nested CTEs during minimum-dependency
validation. The formatter’s semantic-preservation tests remain mandatory.

## Maintenance

`release_routines/python_support.json` owns the accepted range and extra ranges.
After changing it, run `python -m release_routines.lib.python_support sync`.
The offline `check` command rejects generated metadata drift. Validation derives
its matrices from the same policy; minimum dependencies run on the oldest minor
and coverage runs on the middle minor.
Type checks run on the oldest minor so installed dependency stubs match the
language floor, even when newer NumPy releases use newer typing syntax.

The trusted controller checks published Python releases daily and proposes signed
update PRs to `dev`. GitHub probes extra readiness; only results for the exact
candidate, target minor, and current policy can justify a shrink proposal.
Discovery failures preserve accepted support. Independent review and CI remain
required; protected controller changes need exact-head owner approval.

[Documentation overview](README.md)
