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

Service versions are pinned in `integration/docker-compose.yml`. MinIO and its
`mc` client build from the same upstream release commits through
`integration/minio/Dockerfile`: server `RELEASE.2025-09-07T16-13-09Z`
(`07c3a429bfed433e49018cb0f78a52145d4bedeb`) and client
`RELEASE.2025-08-13T08-35-41Z`
(`7394ce0dd2a80935aded936b09fa12cbb3cb8096`). Their upstream container pulls
fail with access denied; switching registry names alone does not establish
availability. Compose builds the pinned Go sources without MinIO registry
credentials, retaining the S3 service, health probe, bucket initialization,
and cleanup commands. The build still requires network access to the Go/Alpine
base images, Go modules, and Alpine packages. Build errors remain startup failures
in `startup.log`. Upstream AGPLv3 licenses are included in each built image;
the corresponding source is available at the pinned upstream commits.

Fast Python guards in `tests/repository/integration_images.py` inspect Compose
and the Dockerfile without Docker or network access. They reject upstream MinIO
image pulls, including auth/fault overrides, and verify the release pins and
runtime commands. GitHub integration validates the actual builds and services.
Data uses temporary filesystems or anonymous Compose state and is removed after
every run.

## Running Locally

Docker Engine with the Compose plugin must be available. Run the stack only
through the repository check entrypoint:

```bash
agent_tools/mcp_tool.sh run-checks --area sql --level integration
```

The default `all` profile is the exhaustive local entrypoint and runs `core`,
`auth`, all destructive `fault` groups, and the resource-intensive `stress`
profile. Every profile runs the identical collected suite once with ClickHouse
HTTP and once with the native protocol. The native extra is required; a missing
`clickhouse-driver` fails before Compose starts. Select one profile explicitly:

```bash
agent_tools/mcp_tool.sh run-checks --area sql --level integration --integration-profile core
agent_tools/mcp_tool.sh run-checks --area sql --level integration --integration-profile auth
agent_tools/mcp_tool.sh run-checks --area sql --level integration --integration-profile fault
agent_tools/mcp_tool.sh run-checks --area sql --level integration --integration-profile stress
```

For transport-specific diagnosis, add
`--integration-clickhouse-driver http` or
`--integration-clickhouse-driver native`. The managed default is `both`, and
the runner fails if the two collections differ. Both transports run even when
HTTP fails, so each attempted transport produces its own diagnostics. The first
transport failure remains the exit status, including teardown or leak-check
failures normalized from the per-transport runner's exit exceptions. Cleanup
failures still allow the other transport to run; later profiles are not started after
a failed profile, and collection parity is checked only when both passes succeed.
This does not change the package's public ClickHouse default, which remains HTTP.

Core covers deterministic database behavior. Auth adds per-run certificates,
HAProxy TLS endpoints, separate Trino Basic and OAuth coordinators, a real
browser-driven Keycloak authorization-code login, secure ClickHouse,
Greenplum mTLS on x86_64, and real Airflow connection and Variable resolution.
Airflow and Playwright are installed only by the auth integration job, never as
package dependencies. Fault starts the complete topology and runs destructive
database, staging, and authentication groups. It is never part of normal
pytest, pre-commit, or `dev` push execution.

Stress proves exact results under simultaneous append and upsert writers,
Greenplum lock contention, Trino connection-pool pressure, and one-million-row
values and Parquet transfers. The streaming cases require batches no larger
than 10,000 rows and process RSS growth below 512 MiB. These bounds are
regression guards for the pinned CI runner, not public memory guarantees.

Core exercises active-query discovery and cancellation, exact logical-type
round trips, all nine source/target transfer and schema-copy pairs, failure
atomicity, both orchestration APIs, and native backend DDL. The canonical type
dataset includes nullable booleans, signed integers, `Decimal(18,4)`, doubles,
Unicode, dates, UTC microsecond timestamps, UUIDs, canonical JSON, and an
all-null text column. Trino/Iceberg and ClickHouse store canonical JSON as text;
Greenplum uses `JSONB`.

The auth topology generates its CA, server, and client credentials for each
run. It covers Greenplum mTLS, Trino Basic TLS and real OAuth authorization-code
login through Chromium and Keycloak, ClickHouse TLS, and Airflow Connection and
Variable routing. Negative cases use wrong credentials and trust roots and
assert that diagnostic output contains no password, private-key, token, or
client-secret material. The OAuth scenarios require the Playwright Chromium
browser installed by the auth job; injected bearer tokens are not accepted.
The browser login helper retries the original OAuth initiation URL up to three
times after a navigation or login-visibility timeout. Reloading the current page
can strand recovery on a blank or failed redirect page after a Keycloak outage.
Service-free harness tests cover both timeout phases and preserve the final
navigation error when all attempts fail. Every negative/fault OAuth browser attempt
appends a scenario-tagged record to `oauth-browser.log`, including its operation
phase, sanitized initiation URL, and the complete exception on failure. Passwords,
client secrets, token values, URL credentials, query strings, and OAuth state paths
are redacted. Browser cleanup runs after navigation, login, callback, and wait
failures and preserves the primary exception if closing also fails. Generic
browser failures are not retried.
The service-free browser regression matrix in
`tests/sql/integration_harness/browser.py` retains all 12 failure/success cases.
Its `_support/browser.py` factory delegates call recording and failure injection
to a private step factory. Focused assertions check the complete operation order,
close counts, primary-error identity, complete redacted diagnostics, secondary-close
failures, and appended scenario history. Additional fake-only cases verify that
close calls are recorded before raising, repeated closes retain exception identity,
and an injected primary close error takes precedence over a secondary close error.

Every scenario uses a profile run ID and test ID in table, stage, object, and
query-label names. The per-test registry cancels labelled queries, drops tables,
removes MinIO objects, and restores paused services before it scans for leaks.
The first-batch database restart scenario holds Greenplum's driver failure inside
the batch retry operation, restores the service, then allows rollback and a fresh
connection retry. It configures two batch attempts: append transfers do not use
the full-transfer retry count. Trino and ClickHouse retain the ambiguous-stage
checkpoint and original-target assertions. A service-free simulation exercises
the scenario with the real batch retry loop and verifies exact rows, a single
failed insert, rollback, and connection replacement.

Fault actions use the same enabled Compose profiles as startup, including `gp`
when Greenplum is enabled; the project-service guard remains active. Before
registry cleanup and leak scans, the fixture reactivates the loopback connection
file to invalidate cached aliases and source settings changed by auth scenarios.
Service-free Python harness tests cover profiled service stop/restore, rejection
of services outside the enabled project, and cleanup after an Airflow source swap.
Cleanup coverage includes both HTTP and native ClickHouse transports, with and
without leaked tables, while preserving cleanup order and leak-report assertions.

The workflow writes `compose.log`, `service-health.json`, `pytest.xml`,
`collected-scenarios.json`, `leaks.json`, `minio-objects.json`,
`active-queries.json`, and `failed-query-details.json` below
`.integration-artifacts/<profile>/<http|native>/`. A profile-level
`transport-parity.json` records the collection comparison. Fault runs also write
`fault-timeline.json`; events retain their scenario IDs across controllers, and
later empty scenarios preserve earlier history. Successful explicit service
restarts discharge their pending fixture recovery action, so cleanup does not
restart a service again after readiness checks. Failed explicit restarts retain
fallback recovery, and other services still restore in reverse order.
Auth runs preserve browser and authentication logs.
Operation/retry and connection-identity reports, orchestration timelines, and
type-normalization mismatch reports are also always present. Stress runs add
memory samples, connection-pressure results, lock timelines, and concurrent
writer results. For startup failures, inspect `startup.log` first. GitHub jobs
print its last 80 lines on failure so captured Compose errors are visible even
when the managed check response contains only an exit status. An early HTTP failure no longer
prevents native startup diagnostics from being collected. An exit status and a
missing native artifact alone do not identify the startup cause; use the captured
Compose error to distinguish stack configuration defects from image, credential,
or runner infrastructure failures before making a repair. Fast, service-free
runner tests cover both transport attempts, the first failure status, teardown,
and stopping later profiles after the failed profile. Cleanup cases group the
HTTP status, failing transport, and expected status into one parametrized tuple
to retain every failure combination within the strict lint argument limit.
These tests run in the normal non-integration suite without external programs.
The startup-diagnostics workflow guard checks all integration execution jobs and
requires the core HTTP/native, auth, fault, and stress suites. Separate policy
jobs may be added without changing the suite count; missing startup reports in
execution jobs still fail fast CI.
Batch diagnostics tests synchronize fake connection failures before asserting
that both running queries failed. A separate sequential case verifies that an
unstarted query remains cancelled with zero attempts and that the original
driver error and connection-source notes are preserved.
For test failures,
inspect service health first, then Compose logs, the operation-specific
timeline, active/failed query details, and finally leak/object reports. It
always runs
`docker compose down --volumes --remove-orphans`, even
when startup or pytest fails, and verifies that project containers, networks,
and volumes are absent. The original test failure remains primary if diagnostic
collection or teardown also fails.

The tests generate `.connections` under pytest's temporary directory and allow
only `127.0.0.1` or `localhost` database hosts. Do not reuse this workflow for a
shared, external, or production database.

## CI

The GitHub agent queues the advisory `sql-integration` workflow once for the
final reviewed PR head before merge. Conflict resolution and repair fixes create
new heads and receive fresh validation. The workflow checks out the immutable
candidate SHA; its run title records that SHA and the PR number. Completion and
success do not gate dev merges. The worker continues monitoring after merge and
repairs failures until the affected groups and transports are green.

Routine push and nightly triggers are disabled. Explicit manual dispatch remains
available for release readiness and individual profiles. Exhaustive candidate
validation includes core, authentication, destructive fault groups, and stress.
Individual tests have a five-minute timeout. Matrix fail-fast is disabled.
Core and auth require zero skipped manifest scenarios on x86_64; ARM runs are
diagnostic and may report Greenplum as architecture-unavailable. All artifacts
are uploaded even on failure. The native auth pass uses separate HAProxy TLS
frontends for the native wire protocol, including hostname-verification
failures. Completion requires no toolkit tables, labelled
queries, MinIO stage objects, project containers, networks, or volumes.

The auth and fault CI jobs install `integration/requirements-auth.txt` without
the optional agent MCP dependency set. Airflow's pinned constraints own that
environment; keeping MCP SDK and its HTTP client requirements out prevents the
test bootstrap from downgrading one dependency set to satisfy the other. The
jobs still run `pip check` before starting services.

Normal commit completion waits only for required fast CI; advisory integration
jobs continue independently. Release readiness reruns and requires the
exhaustive `all` profile across both ClickHouse transports. Any integration
failure that produces a fix must also produce a non-integration regression test
for the same behavior.

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
