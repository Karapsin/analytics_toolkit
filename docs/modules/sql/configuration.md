[SQL module index](index.md)

# Configuration

## General Setup

Connection settings are read from `.connections`. On the first lookup, the
package searches beside the calling Python script and upward through its
parents, then searches from the current working directory upward. Public SQL
functions accept a key from that file; backend behavior is selected from the
key's `type`.

The successful path is remembered for later calls. If that file disappears,
for example after an Airflow worker or DAG path rotation, recovery searches the
remembered file's directory and parents first, then the calling-script and
current-working-directory chains. If a selected file disappears between path
discovery and reading, the package repeats that recovery search up to five
times. Only a missing file triggers recovery; a found file with invalid JSON,
invalid connection settings, a permissions failure, or another I/O error raises
its normal error.

When runtime code cannot rely on the current working directory, set the file
explicitly before calling SQL helpers:

```python
from analytics_toolkit import general, sql

general.set_connections_path("/opt/airflow/dags/project/.connections")
df = sql.read("trino", "select 1")
```

The path must point to an existing `.connections` file. Its directory is used
for relative certificate paths. If it later disappears, the first recovered
file becomes the new explicit path. Call `general.set_connections_path(None)`
to clear the explicit and remembered paths and restart default discovery from
the calling script and current working directory.

Call [sql.generate_dummy_connections](functions/generate_dummy_connections.md)()
to write a starter direct `./.connections` file in the current working
directory. Use
[sql.generate_dummy_connections](functions/generate_dummy_connections.md)(airflow=True)
for an Airflow-source file. The helper never overwrites an existing
`./.connections` file and creates `./.certs/` for local certificate files.

```json
{
  "gp": {
    "type": "gp",
    "host": "gp.example",
    "port": 5432,
    "user": "user",
    "password": "password",
    "database": "db",
    "transfer_staging_schema": "transfer_schema",
    "connect_timeout": 30,
    "keepalives_idle": 60,
    "keepalives_interval": 10,
    "keepalives_count": 3,
    "ca_certs": "gp-ca.pem"
  },
  "gp_sandbox": {
    "type": "gp",
    "host": "gp-sandbox.example",
    "user": "user",
    "password": "password",
    "database": "sandbox"
  },
  "trino": {
    "type": "trino",
    "host": "trino.example",
    "port": 8080,
    "user": "user",
    "password": "password",
    "catalog": "iceberg",
    "schema": "sandbox",
    "transfer_staging_schema": "iceberg.transfer_schema",
    "s3_transfer_staging_schema": "hive.transfer_schema",
    "s3_transfer_staging_location": "s3://m-plus-sandbox/my-prefix/analytics_toolkit",
    "aws_access_key_id": "object-storage-access-key",
    "aws_secret_access_key": "object-storage-secret-key",
    "aws_endpoint_url": "https://storage.yandexcloud.net",
    "http_scheme": "https",
    "ca_certs": "trino-ca.pem",
    "insert_chunk_size": 1000
  },
  "ch": {
    "type": "ch",
    "driver": "http",
    "host": "ch.example",
    "port": 8123,
    "user": "user",
    "password": "password",
    "database": "default",
    "secure": true,
    "transfer_staging_schema": "transfer_schema",
    "ca_certs": "clickhouse-ca.pem",
    "send_receive_timeout": 6000,
    "settings": {"connect_timeout": "500"}
  }
}
```

## Runtime Value References

Any top-level connection field other than routing fields such as `type` and
`connection_id` may resolve its value at runtime from a sibling `.secrets`
file, an environment variable, or an Airflow Variable. Literal values remain
supported, so references can be introduced one credential at a time:

```json
{
  "gp": {
    "type": "gp",
    "host": "gp.example",
    "port": 5432,
    "user": "analytics_user",
    "password": {"from": ".secrets", "key": "GP_PASSWORD"},
    "database": "analytics"
  },
  "trino": {
    "type": "trino",
    "host": "trino.example",
    "user": "analytics_user",
    "aws_access_key_id": {
      "from": "airflow_variable",
      "key": "S3_AF",
      "path": ["credentials", "access_key"]
    },
    "aws_secret_access_key": {
      "from": "airflow_variable",
      "key": "S3_AF",
      "path": ["credentials", "secret_key"]
    }
  }
}
```

The `.secrets` file is always read beside the selected or discovered
`.connections` file. It uses a deliberately strict zsh-compatible assignment
format and is parsed as data; the toolkit never executes it:

```zsh
# .secrets
export GP_PASSWORD='greenplum-password'
export TRINO_PASSWORD='trino-password'
export AWS_ACCESS_KEY_ID='object-storage-access-key'
export AWS_SECRET_ACCESS_KEY='object-storage-secret-key'
export CH_PASSWORD='clickhouse-password'
```

Assignments must use `NAME='value'` or `export NAME='value'` with no spaces
around `=`. Blank lines and full-line comments are accepted. Unquoted values,
double-quoted values, interpolation, commands, duplicate names, and multiline
values are rejected. Add `.secrets` to the consuming project's `.gitignore`.

For that example, Airflow Variable `S3_AF` contains JSON such as:

```json
{
  "credentials": {
    "access_key": "object-storage-access-key",
    "secret_key": "object-storage-secret-key"
  }
}
```

Use `{"from": ".secrets", "key": "NAME"}` for persistent local secrets.
Use `{"from": "env", "key": "NAME"}` for values provided to the process.
Use `{"from": "airflow_variable", "key": "NAME"}` when Airflow supplies a
single scalar value. Add `path` as an array of JSON object keys to parse the
source value as JSON and select a nested value. An empty `path` array selects
the complete parsed JSON value, which is useful for mapping fields such as
ClickHouse `settings`.

To create or complete the persistent file, call
[sql.set_missing_secrets](functions/set_missing_secrets.md)() before using a
connection. It securely prompts once for every absent or empty `.secrets`
reference and writes all collected values atomically:

```python
from analytics_toolkit import sql

sql.set_missing_secrets()
```

The generated entries include `export`, so a trusted file can also be loaded
into zsh with `source /path/to/.secrets`. The toolkit resolves the file
directly, so sourcing it is not required for SQL helpers.

References are resolved in memory on each configuration lookup and are never
written back to `.connections` or cached by the toolkit. Missing sources,
malformed JSON, and missing paths raise a connection-specific `SqlConfigError`
without including the resolved value. Airflow imports remain lazy: ordinary
literal, `.secrets`, and environment-based configurations do not require
Airflow. Passwords and object-storage secrets are omitted from connection
configuration representations.

The existing ClickHouse `ca_certs_variable` field remains supported. The
general equivalent is
`"ca_certs": {"from": "airflow_variable", "key": "ca_certificate"}`.

## Per-Connection DDL Defaults

Each connection may define `ddl_defaults`. `regular` applies to persistent
targets created by `create_table`, create-from-SQL, `load_df`, and
`transfer`, and is the convergence baseline for
`ch_reconfigure_table(..., to_defaults=True)`; `staging` applies to
toolkit-owned stage, worker, upsert, and
materialized-source tables. Trino alone also accepts `parquet_staging`, which
independently configures external Parquet stages.

Precedence is path-specific toolkit defaults, the selected connection scope,
explicit helper arguments, and finally workflow-required properties. Missing
scopes and keys retain Greenplum and Trino behavior. JSON `null` removes an
inherited configurable property within a scope.

```json
{
  "gp": {
    "type": "gp", "host": "gp.example", "user": "user",
    "password": "password", "database": "db",
    "ddl_defaults": {
      "regular": {
        "appendonly": true, "blocksize": 32768,
        "compresstype": "zstd", "compresslevel": 4,
        "orientation": "column"
      },
      "staging": {}
    }
  },
  "trino": {
    "type": "trino", "host": "trino.example", "user": "user",
    "catalog": "iceberg", "schema": "sandbox",
    "ddl_defaults": {
      "regular": {"format": "'PARQUET'", "object_store_layout_enabled": true},
      "staging": {},
      "parquet_staging": {}
    }
  },
  "ch": {
    "type": "ch", "host": "ch.example", "user": "user",
    "password": "password", "database": "default",
    "ddl_ready_timeout_seconds": 300,
    "ddl_ready_timeout_extension_cnt": 1,
    "ch_ddl_wait_policy": "wait_all",
    "ddl_defaults": {
      "regular": {
        "create_distributed_pair": true,
        "shard": {"engine": "ReplicatedMergeTree", "on_cluster": "CORE"},
        "distributed": {
          "engine_template": "Distributed({cluster}, {database}, {shard_table}, {sharding_key})",
          "cluster": "CORE", "on_cluster": "{cluster}",
          "sharding_key": "rand()"
        }
      },
      "staging": {
        "create_distributed_pair": false,
        "shard": {"engine": "MergeTree", "on_cluster": null}
      }
    }
  }
}
```

Direct Trino entries may use either `aws_access_key_id` plus
`aws_secret_access_key`, or `access_key_id` plus `secret_access_key`, for
S3-compatible Parquet staging. Supply one complete family only; incomplete,
mixed, or dual families are rejected. The values are passed only to
`fsspec`/`s3fs` as `key` and `secret` for upload and recursive cleanup, not to
Trino authentication. Omitting credentials preserves the normal AWS provider
chain. Session-token fields are unsupported. Airflow-source `.connections`
files reject literal object-store credentials but accept `env` and
`airflow_variable` references for those fields.

Use either `aws_endpoint_url` or `endpoint_url` for a custom S3-compatible
endpoint. The resolved URL is passed as
`client_kwargs={"endpoint_url": ...}`. Supplying both endpoint names is an
error.

Greenplum and Trino property names are unquoted SQL identifiers and are
normalized to lowercase. Native `true` and the raw string `"true"` both render
as SQL `true`. Numbers render directly. Strings are trimmed and emitted as raw
SQL fragments, so SQL string literals need their own quotes: use
`"'PARQUET'"`, not `"PARQUET"`. Expressions such as `"ARRAY['day']"` remain
valid. JSON arrays render as SQL `ARRAY[...]`, with string elements quoted and
escaped. Empty strings, nested objects, non-finite numbers, invalid keys, and
case-insensitive duplicate keys are rejected.

External Trino Parquet stages apply only `parquet_staging`; they do not inherit
properties from `staging`. They always restore `format = 'PARQUET'` and the
generated `external_location`, because those protected values describe the
files produced by the workflow. Declare a property in both scopes when normal
and external stages both need it.

For ClickHouse, `shard.on_cluster` controls physical-table execution while
`distributed.on_cluster` independently controls facade execution.
`distributed.cluster` is the routing cluster inside `Distributed(...)`; it is
not an execution cluster. Every routing host must contain the physical shard
table with the expected schema; a permanent scope mismatch fails immediately
with routing-host diagnostics instead of waiting out the DDL deadline.
Post-create validation also checks each table on its own execution cluster and
shares one readiness deadline across all polling checks. The
optional connection field `ddl_ready_timeout_seconds` defaults to 300 seconds;
the helpers' `ch_ddl_ready_timeout_seconds` argument takes precedence. For fresh
transfer targets, `ddl_ready_timeout_extension_cnt` defaults to `1` and controls
how many additional `timeout_increment` readiness intervals each finalization
attempt receives. The transfer argument
`ch_ddl_ready_timeout_extension_cnt` takes precedence over the connection.
The optional `ch_ddl_wait_policy` selects which created relations must pass
readiness checks: `wait_all` (default) waits for shard and Distributed tables,
`wait_shard` waits only for physical/shard tables, `wait_distr` waits only for
Distributed facades, and `wait_none` skips post-create waiting. An explicit
function argument takes precedence over the connection value. For a single
physical table, only `wait_all` and `wait_shard` wait.
Templates accept `{cluster}`, `{database}`,
`{shard_table}`, and `{sharding_key}`. The actual target database and generated
shard relation always replace template positions. Explicit
`ch_distributed_cluster` and `ch_sharding_key` replace hardcoded template
arguments, including appending a fourth sharding argument to a three-argument
template. Sharding expressions are preserved verbatim, so integer-valued
ClickHouse expressions such as `rand()` are not rewritten into functions with
different return types. Optional trailing arguments are preserved.

When a replicated shard is created both with `ON CLUSTER` and as a local
visibility fallback, the local statement receives an explicit table UUID. This
keeps zero-argument `ReplicatedMergeTree` compatible with server defaults whose
replica path contains `{uuid}`; clustered DDL continues to coordinate its UUID
inside ClickHouse.

The dedicated ClickHouse overrides are `ch_distributed_engine_template`,
`ch_distributed_cluster`, `ch_shard_on_cluster`, and
`ch_distributed_on_cluster`, alongside nullable `ch_engine`,
`ch_sharding_key`, and `ch_distributed_table`. `ch_only_shard=True` is the
strongest topology override. `ch_cluster` remains a deprecated shortcut that
fills both execution clusters and the routing cluster when dedicated values
are absent.

Older ClickHouse entries that relied on helper defaults must add explicit
`regular` and `staging` policies (the generated dummy file is a working
template) or pass all required helper overrides. Missing required effective
settings fail before dry-run SQL generation or database access.

## Validation

Use [sql.validate_connections](functions/validate_connections.md) to validate
connection files from Python, or use the CLI:

```python
from analytics_toolkit import sql

for result in sql.validate_connections(["gp", "trino"]):
    print(result.connection_key, result.valid, result.error)
```

```bash
analytics-toolkit sql validate
analytics-toolkit sql validate gp trino --connect
analytics-toolkit sql support-matrix
```

## Backend-Specific Direct Options

Greenplum supports optional `connect_timeout`, `keepalives`,
`keepalives_idle`, `keepalives_interval`, `keepalives_count`, `sslmode`,
`ca_certs`, `ssl_cert`, and `ssl_key` fields. It defaults to a 30-second
connection timeout with TCP keepalives enabled. When `ca_certs` is set and
`sslmode` is omitted, Greenplum uses `sslmode="verify-full"`.

Trino supports optional `auth_mode`, `http_scheme`, `verify`, `ca_certs`,
`insert_chunk_size`, `request_timeout`, `source`,
`transfer_staging_schema`, `s3_transfer_staging_schema`, and
`s3_transfer_staging_location` fields. Direct entries also accept the credential
and endpoint families described above.

When `transfer_staging_schema` is set, transfer staging tables owned by that
connection are created under that schema and transfer cleanup scans only
staging tables matching the connection user marker. On a target connection,
this controls the normal load/finalization stage. On a source connection,
row-count validation materializes the source query once in that schema, then
counts and streams the materialized result instead of executing the original
query separately for counting and streaming.

`s3_transfer_staging_schema` and `s3_transfer_staging_location` are an atomic
pair. When both are present, `load_df` and transfers from another connection key
use Parquet files under the object-storage location and create external Trino
stage tables only in the S3 staging schema. `transfer_staging_schema` remains
independent and is used for ordinary SQL staging, including source snapshots and
non-Parquet transfers. Python and Trino must both be able to
write/read/delete the same object-storage prefix.

If the S3 pair is absent, transfers and loads are not Parquet-based:
`transfer_staging_schema` handles SQL staging, Trino transfers use row-batch
`INSERT` staging, and `load_df` uses direct dataframe inserts. The removed
`transfer_staging_location` and `transfer_parquet_staging_schema` names are
rejected; use the `s3_transfer_*` pair.

ClickHouse supports optional `secure`, `verify`, `ca_certs`,
`ca_certs_variable`, `connect_timeout`, `send_receive_timeout`, `settings`,
`ddl_ready_timeout_seconds`, `ddl_ready_timeout_extension_cnt`,
`ch_ddl_wait_policy`, `cluster_routing`, `client_name`, and `compression` fields.
The optional
`driver` selector is
`"http"` by default and keeps the existing `clickhouse-connect` behavior and
default port `8123`. Set `"driver": "native"` to use the native ClickHouse
protocol through the optional `clickhouse-driver` package; its default port is
`9000`. Install it with `pip install 'analytics-toolkit[clickhouse-native]'`.
The native extra does not install Airflow; DAG environments that need both use
`analytics-toolkit[airflow,clickhouse-native]`.

Both transports map `secure`, `verify`, CA certificate settings, connection and
send/receive timeouts, query `settings`, `client_name`, staging schema, and DDL
defaults. Native `compression` defaults to `false` and also accepts `"lz4"` or
`"zstd"`. The fields `interface`, `query_limit`, and `query_retries` are HTTP
only and are rejected for native connections rather than ignored. In
particular, changing only `"interface": "https"` cannot make the HTTP client
connect to a native-protocol port.
`ca_certs_variable` resolves an Airflow Variable lazily when the connection is
opened, which keeps the certificate path in Airflow instead of the file.

### Automatic ClickHouse Cluster Routing

Set `cluster_routing` on a ClickHouse connection when every user query and
toolkit-generated data statement for that alias should target one ClickHouse
cluster. Omitting the field, or setting it to JSON `null`, preserves normal
single-endpoint behavior. The setting works with both HTTP and native drivers.

```json
{
  "clickhouse_clustered": {
    "type": "ch",
    "host": "clickhouse.example",
    "user": "user",
    "password": "password",
    "database": "pa_core_stage",
    "cluster_routing": {
      "cluster": "core",
      "sharding_key": "rand()"
    }
  }
}
```

`cluster` is required. `sharding_key` is optional and defaults to `rand()`.
Both must be non-empty strings, and the sharding key must be one valid
ClickHouse expression. Routing preserves the configured function semantics,
including the integer-returning `rand()` required by `Distributed` sharding;
it does not substitute the floating-point `randCanonical()` function.
Unqualified table names use the connection's `database`; without that field
they fail before execution. Catalog-qualified names and SQL that cannot be
parsed safely also fail closed.

Named query sources are normally rewritten to the
`cluster(cluster, database, table)` table function. This includes nested
queries and named `system` tables; CTE references and existing table functions
are left intact. Text and dataframe inserts normally use
`cluster(cluster, database, table, sharding_key)` as their insert target. Plain
supported DDL receives `ON CLUSTER` automatically.

Managed shard/Distributed pairs receive additional routing. When the named
table is a local `Distributed(...)` facade that points to the same-database
`<table>_shard` relation, the toolkit checks that physical shard on every
replica of the effective routing cluster. Full coverage routes reads directly
through `cluster(..., <table>_shard)` and routes SQL, dataframe, load, and
transfer inserts through the physical shard table function. Explicit INSERT
column lists are preserved. If full coverage cannot be proved, including when
the topology probe fails, both reads and writes use the local Distributed
facade instead. A similarly named table that is not an exact managed pair keeps
the normal routing behavior.

This makes `ch_ddl_wait_policy="wait_shard"` compatible with a fully deployed
managed shard on the routing cluster because ordinary data operations do not
depend on Distributed-facade readiness. The incomplete-coverage fallback still
requires the facade on the connected host to exist and be usable; `wait_shard`
does not promise facade readiness. Route decisions are cached per connection
and cleared by table DDL.

`sql.transfer` uses non-replicated scratch tables with cluster routing. Keep the
staging policy on a single physical `MergeTree` and do not create a Distributed
pair:

```json
"staging": {
  "create_distributed_pair": false,
  "shard": {"engine": "MergeTree", "on_cluster": null}
}
```

The toolkit deploys those private stages on the routing cluster and reads
reserved transfer-stage sources through `clusterAllReplicas(...)`. Target
writers distribute batches normally. Source snapshots are created empty on
every replica, held behind a shard-readiness barrier, and then populated once
through `cluster(...)`; this keeps the snapshot reconnect-safe without creating
one copy per replica. Regular tables continue to use `cluster(...)`. A
replicated staging engine or staging Distributed pair on either the source or
target connection is rejected before the transfer opens database connections
because all-replica reads would duplicate replicated rows. Toolkit-owned routed
stages always wait for shard readiness even when the connection selects a
weaker wait policy for ordinary DDL.

An explicit DDL scope always wins over `cluster_routing`. For example,
`ON CLUSTER '{cluster}'` remains exactly that macro and is also used to route
table sources inside the same statement. This lets connection-level routing
coexist with the `on_cluster` values selected by `ddl_defaults` or explicit
helper arguments. Toolkit topology probes, readiness checks, cancellation, and
intentional local fallback DDL remain local control operations.

Dry-run plans expose the routed SQL when their statements are valid executable
SQL. Existing `cluster(...)` functions are idempotent, so prepared SQL can pass
through the connection wrapper without being nested a second time.

All backends support optional `transfer_staging_schema` for transfer staging
tables. When omitted on a target connection, transfer staging defaults to
per-connection legacy naming in the target table namespace. When omitted on a
source connection, row-count validation retains the direct count-then-stream
behavior.

For Greenplum, Trino, and ClickHouse, `ca_certs` accepts one certificate file
name/path or a list of certificate file names/paths. A bare name such as
`trino-ca.pem` resolves to `.certs/trino-ca.pem` next to `.connections`; a
relative path such as `.certs/trino-ca.pem` resolves relative to the
`.connections` directory; an absolute path is used as-is. Multiple CA files are
bundled into a generated PEM bundle under `.certs/.generated/`. Missing
certificate files are reported when the connection is opened.

Authentication is established by the backend driver, not by accepting a
prebuilt bearer token from toolkit callers. Greenplum supports password TLS and
client-certificate fields (`ssl_cert`/`ssl_key`); Trino supports Basic TLS and
`auth_mode="oauth2"`; ClickHouse supports direct TLS and Airflow Variable CA
resolution. OAuth follows the Trino driver's browser callback and verifies both
the Trino and identity-provider TLS chains. Logs may include the authorization
URL and phase name, but credentials, token values, client secrets, and private
key contents are redacted.

## Airflow-Source Connections

In Airflow DAGs, keep credentials in Airflow Connections and use an
Airflow-source `.connections` file with routing metadata only:

```json
{
  "source": "airflow",
  "connections": {
    "airflow_gp": {"type": "gp"},
    "airflow_trino": {"type": "trino"},
    "airflow_clickhouse": {"type": "ch"}
  }
}
```

The connection key is used as the Airflow connection ID by default. Use
`connection_id` when the toolkit alias should differ from the Airflow ID:

```json
{
  "source": "airflow",
  "connections": {
    "trino": {
      "connection_id": "airflow_trino",
      "type": "trino",
      "insert_chunk_size": 1000,
      "http_scheme": {"from": "extra", "fallback": "https"},
      "verify": {"from": "extra", "fallback": false},
      "request_timeout": {"from": "extra", "fallback": 600},
      "source": {"from": "extra", "fallback": "analytics_toolkit"}
    },
    "clickhouse": {
      "connection_id": "airflow_clickhouse",
      "type": "ch",
      "ca_certs_variable": "clickhouse_ca_cert"
    }
  }
}
```

Airflow-source entries support resolver objects for optional connection extras.
Use `{"from": "extra", "fallback": VALUE}` to read the same-named Airflow
`extra_dejson` key with a fallback, or add `"key": "other_name"` to read a
different Airflow extra. Plain values still force a file-level override.
The general `env` and `airflow_variable` references described above may be used
in the same Airflow-source entry, including for object-storage credentials.
`ddl_defaults` is a literal nested mapping, even when a DDL property is named
`from`. The whole object may also come from Airflow extras with an explicit
resolver, for example
`"ddl_defaults": {"from": "extra", "key": "ddl_defaults"}`; a literal
mapping with additional scope keys is never mistaken for a resolver.

The Airflow Connection's explicit port overrides the driver default. For
example, this migration keeps host, port `9003`, login, password, and database
in Airflow while selecting the native transport in the routing file:

```json
{
  "source": "airflow",
  "connections": {
    "clickhouse_pa_core": {
      "type": "ch",
      "driver": "native",
      "ca_certs_variable": "ca_certificate",
      "send_receive_timeout": 6000,
      "ddl_ready_timeout_seconds": 600,
      "ddl_ready_timeout_extension_cnt": 1,
      "settings": {
        "connect_timeout": "500"
      }
    }
  }
}
```

This selects the native transport while credentials and the explicit port still
come from Airflow. `driver` and `compression` may also use Airflow-extra
resolver objects.

Airflow task working directories can differ from the DAG project root. If the
default search from the current working directory upward cannot find the
Airflow-source file, call `general.set_connections_path("/path/to/.connections")`
before the SQL helper call.

Once this file is present, DAG code can call SQL helpers directly. The common
entrypoints are [sql.execute](functions/execute.md),
[sql.read](functions/read.md), and [sql.transfer](functions/transfer.md):

```python
from analytics_toolkit import sql

sql.execute("airflow_trino", "select 1", query_label="healthcheck")
df = sql.read("airflow_gp", "select * from sandbox.table")
sql.transfer(
    from_db="airflow_trino",
    to_db="airflow_clickhouse",
    from_sql="select * from iceberg.sandbox.source",
    to_table="sandbox.target",
    write_mode="replace",
)
```

[SQL module index](index.md)
