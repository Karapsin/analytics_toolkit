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
`./.connections` file.

```json
{
  "gp": {
    "type": "gp",
    "host": "gp.example",
    "port": 5432,
    "user": "user",
    "password": "password",
    "database": "db",
    "connect_timeout": 30,
    "keepalives_idle": 60,
    "keepalives_interval": 10,
    "keepalives_count": 3
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
    "ca_cert": "/path/to/ca.pem",
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
`keepalives_idle`, `keepalives_interval`, and `keepalives_count` fields. They
default to a 30-second connection timeout with TCP keepalives enabled.

Trino supports optional `auth_mode`, `http_scheme`, `verify`,
`use_keychain_certs`, `keychain_cert_names`, `insert_chunk_size`,
`request_timeout`, and `source` fields.

ClickHouse supports optional `secure`, `verify`, `ca_cert` / `ca_certs`,
`ca_cert_variable` / `ca_certs_variable`, `connect_timeout`,
`send_receive_timeout`, `settings`, `interface`, `query_limit`,
`query_retries`, and `client_name` fields. `ca_cert_variable` resolves an
Airflow Variable lazily when the connection is opened, which keeps the
certificate path in Airflow instead of the file.

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

[sql.airflow_connection_config](functions/airflow_connection_config.md) maps
one Airflow Connection to the same config objects used by `.connections`. If
`backend` is omitted, the package infers it from Airflow `conn_type` or extra
`type` / `backend`. Greenplum and ClickHouse use the Airflow `schema` field as
the database. Trino uses `catalog`, `schema`, `auth_mode`, `http_scheme`,
`verify`, `insert_chunk_size`, `request_timeout`, and `source` from connection
extras. ClickHouse uses the fields listed above from connection extras and
defaults Airflow-source connections to `send_receive_timeout=6000` and
`settings={"connect_timeout": "500"}` when those fields are not provided.

Use [sql.use_airflow_connections](functions/use_airflow_connections.md) when
Python code should temporarily resolve configured connection IDs through
Airflow instead of a local `.connections` file.

[SQL module index](index.md)
