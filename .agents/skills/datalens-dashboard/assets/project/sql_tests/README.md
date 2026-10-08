# Independent numerical checks

Every SQL file contains the actual source table and runs directly when pasted
into ClickHouse. `-- SELECTOR_FILTERS` is a comment for the cleared query; the
runner inserts scenario conditions there. Keep source names aligned with the
dataset configuration and update physical columns when changing the source.

Use the analytics-toolkit skill, public sql.read and the existing external
analytics interpreter. Obtain permission to analyze data transiently before
running this command:

```sh
python -B -m sql_tests.run --db-key ch
```

The runner verifies published metadata first and compares cleared/default,
multiple-region and empty-match states across the three charts. It compares
Wizard dataset results, bound published QL SQL and published Editor code with
independent SQL. Raw rows remain transient; scenario status, revisions and
tolerances are saved in the external runtime. Missing SQL access is recorded as
blocked. These checks do not execute browser events or rendered charts.

Always batch DataLens API queries wherever public operations support batching.
Inventories gather all IDs; compatible measures share a query only at identical
grain and filter state. Cache exact repeats and serialize unsupported batch
operations with shared pacing across clients using the same organization.

The default analytics interpreter is the current Python executable with the
already installed analytics-toolkit[all]. An explicit --analytics-python may
select another existing environment; never create a project environment.
