[SQL module index](index.md)

# Configuration

## General Setup

Connection settings are read from `.connections`. The package searches from
the current working directory upward through parent directories. Public SQL
functions accept a key from that file; backend behavior is selected from the
key's `type`.

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
    "transfer_staging_schema": "transfer_schema",
    "transfer_staging_location": "s3://bucket/tmp/analytics_toolkit_transfer",
    "http_scheme": "https",
    "ca_certs": "trino-ca.pem",
    "insert_chunk_size": 1000
  },
  "ch": {
    "type": "ch",
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
`transfer_staging_schema`, and `transfer_staging_location` fields.

When `transfer_staging_schema` is set, transfer staging tables for that
connection are created under that schema and transfer cleanup scans only
staging tables matching the target transfer user marker.

When a Trino target defines both `transfer_staging_schema` and
`transfer_staging_location`, transfers from a different connection key stage
source rows as Parquet files under the object-storage location and create a
temporary Trino table in `transfer_staging_schema` over that prefix. Python and
Trino must both be able to write/read/delete the same object-storage prefix.
Install `analytics-toolkit[parquet-transfer]` in Python environments that use
this fast path. If `transfer_staging_location` is omitted, Trino transfers keep
using row-batch `INSERT` staging.

ClickHouse supports optional `secure`, `verify`, `ca_certs`,
`ca_certs_variable`, `connect_timeout`, `send_receive_timeout`, `settings`,
`interface`, `query_limit`, `query_retries`, and `client_name` fields.
`ca_certs_variable` resolves an Airflow Variable lazily when the connection is
opened, which keeps the certificate path in Airflow instead of the file.

All backends support optional `transfer_staging_schema` for transfer staging tables.
When omitted, transfer staging defaults to per-connection legacy naming in the
target table namespace.

For Greenplum, Trino, and ClickHouse, `ca_certs` accepts one certificate file
name/path or a list of certificate file names/paths. A bare name such as
`trino-ca.pem` resolves to `.certs/trino-ca.pem` next to `.connections`; a
relative path such as `.certs/trino-ca.pem` resolves relative to the
`.connections` directory; an absolute path is used as-is. Multiple CA files are
bundled into a generated PEM bundle under `.certs/.generated/`. Missing
certificate files are reported when the connection is opened.

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
)
```

[SQL module index](index.md)
