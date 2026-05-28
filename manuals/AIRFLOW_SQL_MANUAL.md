# Airflow SQL Manual

Use this manual when migrating DAG code from local Airflow connection wrappers to
`analytics_toolkit.sql`.

The intended Airflow pattern is:

- keep credentials in Airflow Connections;
- keep only routing metadata and optional connector overrides in `.connections`;
- call `sql.read`, `sql.execute`, `sql.load_df`, and `sql.transfer` directly from
  DAG tasks.

## Airflow `.connections`

Put an Airflow-source `.connections` file in a directory visible from the task
working directory, usually the DAG project root:

```json
{
  "source": "airflow",
  "connections": {
    "airflow_gp": {
      "type": "gp",
      "connect_timeout": 5,
      "keepalives": true,
      "keepalives_idle": 1,
      "keepalives_interval": 1,
      "keepalives_count": 20
    },
    "airflow_trino": {
      "type": "trino",
      "http_scheme": {"from": "extra", "fallback": "https"},
      "verify": {"from": "extra", "fallback": false},
      "request_timeout": {"from": "extra", "fallback": 600},
      "source": {"from": "extra", "fallback": "airflow"}
    },
    "airflow_clickhouse": {
      "type": "ch",
      "secure": true,
      "ca_certs_variable": "clickhouse_ca_cert",
      "send_receive_timeout": 6000,
      "settings": {"connect_timeout": "500"}
    }
  }
}
```

Use resolver objects when old DAG wrappers used `extra.get("field", fallback)`:

```json
{
  "http_scheme": {"from": "extra", "fallback": "https"},
  "verify": {"from": "extra", "fallback": false},
  "request_timeout": {"from": "extra", "fallback": 300},
  "source": {"from": "extra", "fallback": "airflow-trino"}
}
```

The resolver reads the same-named Airflow Connection `extra_dejson` key when it
exists and is not `null`; otherwise it uses `fallback`. Add `key` when the
Airflow extra key has a different name:

```json
{
  "source": {
    "from": "extra",
    "key": "client_source",
    "fallback": "airflow-trino"
  }
}
```

Plain values still force an override, for example `"request_timeout": 900`.

The key is the Airflow connection ID by default. Use `connection_id` when the
toolkit alias should be different:

```json
{
  "source": "airflow",
  "connections": {
    "trino": {
      "connection_id": "airflow_trino",
      "type": "trino"
    }
  }
}
```

## Greenplum

Old wrapper style:

```python
from legacy_dag_utils.postgres import postgres_conn

query = """
delete from sandbox.daily_result
where dt = current_date;
"""

with postgres_conn("airflow_gp") as conn:
    with conn.cursor() as cur:
        cur.execute(query)
    conn.commit()
```

New toolkit style:

```python
from analytics_toolkit import sql

query = """
delete from sandbox.daily_result
where dt = current_date;
"""

sql.execute("airflow_gp", query, query_label="delete_daily_result")
```

Old dataframe read:

```python
import pandas as pd
from legacy_dag_utils.postgres import postgres_conn

with postgres_conn("airflow_gp") as conn:
    df = pd.read_sql_query(
        "select * from sandbox.daily_result where dt = current_date",
        conn,
    )
```

New dataframe read:

```python
from analytics_toolkit import sql

df = sql.read(
    "airflow_gp",
    "select * from sandbox.daily_result where dt = current_date",
    query_label="read_daily_result",
)
```

## Trino

Old wrapper style:

```python
from airflow.hooks.base import BaseHook
from trino.auth import BasicAuthentication
from trino.dbapi import connect

conn_params = BaseHook.get_connection("airflow_trino")
extra = conn_params.extra_dejson

conn = connect(
    host=conn_params.host,
    port=conn_params.port,
    auth=BasicAuthentication(conn_params.login, conn_params.password),
    http_scheme=extra.get("http_scheme", "https"),
    user=conn_params.login,
    request_timeout=extra.get("request_timeout", 600),
    verify=extra.get("verify", False),
    source=extra.get("source", "airflow"),
    catalog=extra.get("catalog"),
    schema=extra.get("schema"),
)
try:
    cur = conn.cursor()
    cur.execute("insert into iceberg.sandbox.target select * from source_table")
finally:
    conn.close()
```

New toolkit style:

```python
from analytics_toolkit import sql

sql.execute(
    "airflow_trino",
    "insert into iceberg.sandbox.target select * from source_table",
    query_label="insert_target",
)
```

Old dataframe read:

```python
import pandas as pd

cur = conn.cursor()
cur.execute("select user_id, amount from iceberg.sandbox.source")
df = pd.DataFrame(cur.fetchall(), columns=[col[0] for col in cur.description])
```

New dataframe read:

```python
from analytics_toolkit import sql

df = sql.read(
    "airflow_trino",
    "select user_id, amount from iceberg.sandbox.source",
    query_label="read_source",
)
```

## ClickHouse

Old wrapper style:

```python
from legacy_dag_utils.clickhouse import clickhouse_conn

with clickhouse_conn(
    clickhouse_conn_id="airflow_clickhouse",
    settings={"use_numpy": True},
) as client:
    client.execute(
        """
        insert into sandbox.target
        select *
        from sandbox.source
        """
    )
```

New toolkit style:

```python
from analytics_toolkit import sql

sql.execute(
    "airflow_clickhouse",
    """
    insert into sandbox.target
    select *
    from sandbox.source
    """,
    query_label="insert_clickhouse_target",
)
```

Old dataframe read:

```python
from legacy_dag_utils.clickhouse import clickhouse_conn

with clickhouse_conn("airflow_clickhouse") as client:
    df = client.query_dataframe("select * from sandbox.source")
```

New dataframe read:

```python
from analytics_toolkit import sql

df = sql.read(
    "airflow_clickhouse",
    "select * from sandbox.source",
    query_label="read_clickhouse_source",
)
```

## Loading DataFrames

Old style usually opens a backend client and manages table creation, batches,
and retries in DAG code.

```python
from legacy_dag_utils.clickhouse import clickhouse_conn

with clickhouse_conn("airflow_clickhouse", settings={"use_numpy": True}) as client:
    client.execute("truncate table sandbox.target")
    client.insert_dataframe("insert into sandbox.target values", df)
```

New toolkit style:

```python
from analytics_toolkit import sql

sql.load_df(
    "airflow_clickhouse",
    "sandbox.target",
    df,
    write_mode="replace",
    partition_by="dt",
    order_by=["dt", "user_id"],
)
```

For Greenplum:

```python
sql.load_df(
    "airflow_gp",
    "sandbox.target",
    df,
    write_mode="replace",
    gp_distributed_by_key=["user_id"],
)
```

For Trino:

```python
sql.load_df(
    "airflow_trino",
    "iceberg.sandbox.target",
    df,
    write_mode="replace",
)
```

## Transfers

Old style reads from one client and writes to another inside DAG code.

```python
import pandas as pd
from legacy_dag_utils.postgres import postgres_conn
from legacy_dag_utils.clickhouse import clickhouse_conn

with postgres_conn("airflow_gp") as gp_conn:
    df = pd.read_sql_query("select * from sandbox.source", gp_conn)

with clickhouse_conn("airflow_clickhouse", settings={"use_numpy": True}) as ch:
    ch.execute("truncate table sandbox.target")
    ch.insert_dataframe("insert into sandbox.target values", df)
```

New toolkit style:

```python
from analytics_toolkit import sql

sql.transfer(
    from_db="airflow_gp",
    to_db="airflow_clickhouse",
    from_sql="select * from sandbox.source",
    to_table="sandbox.target",
    replace_target_table=True,
    key_columns=["user_id", "dt"],
    partition_by="dt",
    order_by=["dt", "user_id"],
)
```

The transfer helper manages staged loading, retries, batching, and backend
specific table creation. Use it instead of moving large data through handwritten
cursor loops when both source and target are supported SQL backends. For
memory-constrained Airflow workers, pass `target_batch_memory_mb` so adaptive
batches target row batch memory instead of insert duration. Memory-targeted
transfers have no default `max_batch_size` ceiling; pass `max_batch_size` when
the Airflow task should enforce a hard row-count cap.

## Airflow Task Examples

TaskFlow:

```python
from airflow.decorators import task
from analytics_toolkit import sql


@task
def refresh_table() -> None:
    sql.execute(
        "airflow_trino",
        "insert into iceberg.sandbox.target select * from iceberg.sandbox.source",
        query_label="refresh_target",
    )
```

`PythonOperator`:

```python
from airflow.operators.python import PythonOperator
from analytics_toolkit import sql


def refresh_table() -> None:
    sql.transfer(
        from_db="airflow_trino",
        to_db="airflow_clickhouse",
        from_sql="select * from iceberg.sandbox.source",
        to_table="sandbox.target",
        replace_target_table=True,
        target_batch_memory_mb=256,
        partition_by="dt",
        order_by=["dt", "user_id"],
    )


refresh = PythonOperator(
    task_id="refresh_table",
    python_callable=refresh_table,
)
```

## What Not To Replace Yet

Keep specialized DAG utilities when the toolkit does not own the behavior yet:

- ClickHouse mutation sensors and mutation-specific polling;
- S3-specific import/export operators;
- custom Airflow operators with non-SQL side effects;
- database clients needed for APIs outside `analytics_toolkit.sql`.

For plain SQL execution, dataframe reads, dataframe loads, and supported
backend-to-backend transfers, prefer the toolkit calls shown above.
