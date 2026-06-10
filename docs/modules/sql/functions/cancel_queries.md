[SQL functions index](index.md)

# cancel_queries

Cancel running queries on Greenplum, Trino, or ClickHouse.

```python
cancel_queries(db_key: 'str', query_ids: 'int | str | Sequence[int | str] | None' = None, *, cancel_all: 'bool' = False, concurrency: 'int' = 1, print_queries: 'bool' = False, retry_cnt: 'int' = 5, timeout_increment: 'int | float' = 5, query_label: 'str | None' = None) -> 'pd.DataFrame'
```

## Inputs

- `db_key` - connection key or alias from `.connections`, or an Airflow connection ID when Airflow routing is active
- `query_ids` - one query id or a sequence of query ids to cancel. For Greenplum, ids are backend PIDs from `pg_stat_activity`
- `cancel_all` - set to `True` to cancel current-user running queries for the configured connection. Do not provide `query_ids` at the same time
- `concurrency` - maximum requested cancellation concurrency
- `print_queries` - print generated cancellation SQL before execution
- `retry_cnt` - number of operation retries with fresh connections
- `timeout_increment` - delay increment used between operation retry attempts
- `query_label` - optional label added to SQL comments for observability

## Usage

```python
from analytics_toolkit import sql

cancelled = sql.cancel_queries("trino", ["20260610_120000_00001_abcd1"])
print(cancelled)

cancelled_all = sql.cancel_queries("gp", cancel_all=True, concurrency=4)
print(cancelled_all)
```

Output example:

```python
cancelled[["backend", "query_id", "cancelled"]]
#   backend                   query_id  cancelled
# 0   trino  20260610_120000_00001_abcd1       True
```

## Notes

- Exactly one cancellation mode is required: provide `query_ids` or set `cancel_all=True`.
- `cancel_all=True` targets current-user queries and excludes the helper's own session where the backend exposes that information.
- Returned rows include the backend, target query id, generated cancellation SQL, cancellation flag, and backend status.

[SQL functions index](index.md)
