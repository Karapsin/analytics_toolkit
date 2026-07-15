[Documentation overview](README.md)

# Disposable SQL Integration Tests

The integration workflow validates `analytics_toolkit.sql` against disposable
Greenplum, Trino, and ClickHouse services. It is separate from the deterministic
unit, coverage, and pre-commit suites, which continue to use fake connections.

## Services

The default stack runs Trino with a writable Iceberg catalog backed by
PostgreSQL and MinIO. ClickHouse runs with ClickHouse Keeper as a one-node named
cluster so distributed and replicated table behavior is available. On x86_64,
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

The default `all` profile runs `core` and `auth`. Select one profile explicitly:

```bash
agent_tools/mcp_tool.sh run-checks --area sql --level integration --integration-profile core
agent_tools/mcp_tool.sh run-checks --area sql --level integration --integration-profile auth
agent_tools/mcp_tool.sh run-checks --area sql --level integration --integration-profile fault
```

Core covers deterministic database behavior. Auth adds per-run certificates,
HAProxy TLS endpoints, Trino password authentication, Keycloak realm fixtures,
secure ClickHouse, Greenplum client certificates on x86_64, and real Airflow
connection resolution. Fault is reserved for destructive restart/retry cases.

The workflow writes Compose logs, service health snapshots, JUnit reports, and
leak reports to `.integration-artifacts/<profile>/`. It always runs
`docker compose down --volumes --remove-orphans`, even
when startup or pytest fails.

The tests generate `.connections` under pytest's temporary directory and allow
only `127.0.0.1` or `localhost` database hosts. Do not reuse this workflow for a
shared, external, or production database.

## CI

The `sql-integration` workflow runs required core and auth x86_64 jobs in
parallel on every push to `dev`, each with a 60-minute limit. The destructive
fault profile runs nightly and by manual dispatch. All profile artifacts are
uploaded even when tests pass. Completion requires no toolkit tables, labelled
queries, MinIO stage objects, project containers, or project volumes left behind.

The machine-readable coverage declaration is
`integration/sql_coverage_manifest.json`. Its guard tests compare public SQL
exports, callable parameters, registered adapters, supported write modes,
source/target pairs, and collected scenario references so additions cannot be
silently omitted. Unsupported Kerberos and JWT routes are explicitly excluded
because the toolkit does not expose them.

[Documentation overview](README.md)
