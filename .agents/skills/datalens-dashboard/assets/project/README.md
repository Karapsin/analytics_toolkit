# Dashboard project

The starter demonstrates one dataset, three chart families and their explicit
region selector mechanisms. Retain only required families and update coverage.
The source is @TABLE@ with event_date, region and revenue columns; adapt field
mapping, projection and independent SQL to the real schema. Replace North/South
manual options and scenario values with meaningful source values.

Use the current Python environment, which already has analytics-toolkit[all]
and its dependencies installed. The engine is analytics_toolkit.datalens_utils.
No virtual environment, installation, lockfile or bootstrap is needed in this
project. Reuse the existing YC CLI/profile; DATALENS_YC_BIN can select its path.

Run `python dashboard.py validate`, then `python dashboard.py` for an authorized
deployment. Use status, scoped apply/pull, and verify --full for later changes.
Checkpoints and exports use an external sibling .local runtime directory;
DATALENS_RUNTIME_DIR can override it. Login remains user-owned.

With the resolved interpreter, run `python -B -m unittest discover -s tests` and
`node assets/js/verify.mjs`. Independent SQL tests use the same installed
interpreter: `python -B -m sql_tests.run`.
Read sql_tests/README.md first and obtain permission for transient data analysis.
API/code checks do not prove rendered output or browser parameter propagation.

Always batch DataLens API queries when public operations support batching.
Use bulk/paginated inventories and compatible dataset queries without changing
grain or filters. Cache exact repeats and share pacing across clients in one
organization for operations without a public batch method. Concurrent requests
and caching alone do not constitute API batching.
