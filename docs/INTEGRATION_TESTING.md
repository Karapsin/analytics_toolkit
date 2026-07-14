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

The workflow writes diagnostics to `.integration-artifacts/compose.log` after
a failure. It always runs `docker compose down --volumes --remove-orphans`, even
when startup or pytest fails.

The tests generate `.connections` under pytest's temporary directory and allow
only `127.0.0.1` or `localhost` database hosts. Do not reuse this workflow for a
shared, external, or production database.

## CI

The `sql-integration` GitHub Actions workflow runs the complete x86_64 stack on
every push to `dev` and supports manual dispatch. Superseded `dev` runs are
cancelled, and Compose logs are uploaded when the check fails.

[Documentation overview](README.md)
