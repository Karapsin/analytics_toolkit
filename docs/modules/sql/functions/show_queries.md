[SQL functions index](index.md)

# show_queries

List backend queries visible to a configured connection.

```python
show_queries(db_key: 'str', *, user: 'str | None' = None, state: 'str | SequenceABC[str]' = 'active', print_queries: 'bool' = False, retry_cnt: 'int' = 5, timeout_increment: 'int | float' = 5, query_label: 'str | None' = None) -> 'pd.DataFrame'
```

## Inputs

- `db_key` - connection key or alias from `.connections`
- `user` - backend user to filter; omitted values use the backend current user for that connection
- `state` - query state filter: `active`, `finished`, `failed`, `all`, or a sequence of those values
- `print_queries` - whether to print the metadata SQL before execution
- `retry_cnt` - number of operation retries with fresh connections
- `timeout_increment` - delay increment used between operation retry attempts
- `query_label` - optional label added to SQL comments for observability

## Usage

```python
from analytics_toolkit import sql

running = sql.show_queries("trino")
print(running[["backend", "query_id", "state", "query"]])

recent = sql.show_queries("ch", state=["active", "failed"])
print(recent)
```

## Notes

- Returned rows use normalized columns: `backend`, `query_id`, `user`, `state`,
  `query`, `started_at`, `finished_at`, `elapsed_seconds`, `source`,
  `database`, and `raw_state`.
- By default, `show_queries` lists active queries for the backend current user.
- `user` filters another backend user when that user is visible to the
  configured connection.
- Historical states are best effort. Greenplum exposes only active
  `pg_stat_activity` sessions through this helper. Trino and ClickHouse history
  depend on backend retention, system table availability, and permissions.
- [cancel_queries](cancel_queries.md) uses this helper to discover active
  current-user query IDs when `cancel_all=True`.

[SQL functions index](index.md)
