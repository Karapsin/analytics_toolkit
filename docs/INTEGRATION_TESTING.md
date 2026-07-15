[Documentation overview](README.md)

# Disposable SQL Integration Tests

The integration workflow validates `analytics_toolkit.sql` against disposable
Greenplum, Trino, and ClickHouse services. It is separate from the deterministic
unit, coverage, and pre-commit suites, which continue to use fake connections.

## Services

The default stack runs Trino with a writable Iceberg target catalog backed by
PostgreSQL and MinIO plus a Hive metastore catalog for external Parquet stages.
ClickHouse runs with ClickHouse Keeper as a one-node named cluster so
distributed and replicated table behavior is available. On x86_64,
the same workflow also enables a Greenplum 6 profile. Apple Silicon runs the
Trino and ClickHouse portions without emulating Greenplum.

All service versions are pinned in `integration/docker-compose.yml`. Data uses
temporary filesystems or anonymous Compose state and is removed after every
run.

## Running Locally

Docker Engine with the Compose plugin must be available. Run the stack only
through the repository check entrypoint:

```bash
agent_tools/mcp_tool.sh run-checks --area sql --level integration
```

The default `all` profile is the exhaustive local entrypoint and runs `core`,
`auth`, and all destructive `fault` groups. Select one profile explicitly:

```bash
agent_tools/mcp_tool.sh run-checks --area sql --level integration --integration-profile core
agent_tools/mcp_tool.sh run-checks --area sql --level integration --integration-profile auth
agent_tools/mcp_tool.sh run-checks --area sql --level integration --integration-profile fault
```

Core covers deterministic database behavior. Auth adds per-run certificates,
HAProxy TLS endpoints, separate Trino Basic and OAuth coordinators, a real
browser-driven Keycloak authorization-code login, secure ClickHouse,
Greenplum mTLS on x86_64, and real Airflow connection and Variable resolution.
Airflow and Playwright are installed only by the auth integration job, never as
package dependencies. Fault starts the complete topology and runs destructive
database, staging, and authentication groups. It is never part of normal
pytest, pre-commit, or `dev` push execution.

Every scenario uses a profile run ID and test ID in table, stage, object, and
query-label names. The per-test registry cancels labelled queries, drops tables,
removes MinIO objects, and restores paused services before it scans for leaks.

The workflow writes `compose.log`, `service-health.json`, `pytest.xml`,
`collected-scenarios.json`, `leaks.json`, `minio-objects.json`,
`active-queries.json`, and `failed-query-details.json` below
`.integration-artifacts/<profile>/`. Fault runs also write
`fault-timeline.json`; auth runs preserve browser and authentication logs. It
always runs
`docker compose down --volumes --remove-orphans`, even
when startup or pytest fails, and verifies that project containers, networks,
and volumes are absent. The original test failure remains primary if diagnostic
collection or teardown also fails.

The tests generate `.connections` under pytest's temporary directory and allow
only `127.0.0.1` or `localhost` database hosts. Do not reuse this workflow for a
shared, external, or production database.

## CI

The `sql-integration` workflow runs required core and auth x86_64 jobs in
parallel on every push to `dev`, each with a 60-minute limit. The destructive
fault groups run nightly and by manual dispatch with matrix fail-fast disabled.
Core and auth require zero skipped manifest scenarios on x86_64; ARM runs are
diagnostic and may report Greenplum as architecture-unavailable. All artifacts
are uploaded even on failure. Completion requires no toolkit tables, labelled
queries, MinIO stage objects, project containers, networks, or volumes.

The machine-readable coverage declaration is
`integration/sql_coverage_manifest.json`. Its guard tests compare public SQL
exports, callable parameters, registered adapters, supported write modes,
source/target pairs, aliases, orchestration task types, exact unit references,
and collected scenario markers so additions cannot be silently omitted. A
manifest scenario that is not collected, or a collected scenario absent from
the manifest, fails the guard. Unsupported Kerberos and JWT routes remain
explicit capability-tested exclusions because the toolkit does not expose them.

For a failed service, start with `service-health.json`, then the matching section
of `compose.log`; query and object leaks have dedicated reports. Do not rerun a
failed CI job until the logs demonstrate a transient infrastructure failure.
After any corrective push, verify the new immutable SHA. If a check watch is
interrupted, resume only with:

```text
git_workflow(action="checks", sha="<exact-pushed-sha>")
```

Never use the newest branch run as a proxy for the pushed SHA.

[Documentation overview](README.md)
