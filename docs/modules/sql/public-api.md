[SQL module index](index.md)

# Public API

```python
from analytics_toolkit import sql

# General helpers
sql.read(..., retry_cnt=5, timeout_increment=5)
sql.execute(..., retry_cnt=5, timeout_increment=5, progress=False)
sql.execute_read(..., retry_cnt=5, timeout_increment=5, progress=False)
sql.load_df(..., retry_cnt=5, timeout_increment=5, progress=False)
sql.transfer(
    ...,
    batch_size=100_000,
    adaptive_batch_size=True,
    target_batch_memory_mb=None,
    progress=False,
)
sql.table_info(...)
sql.show_tables(...)
sql.create_table_from_sql(...)
sql.create_sql_table(...)
sql.extract_ddl(...)
sql.drop_many_partitions(...)
sql.format_plan(...)
sql.async_sql(
    ...,
    concurrency=5,
    fail_fast=True,
    soft_concurrency_cap=None,
    hard_concurrency_cap=10,
    progress=False,
)
sql.parallel_sql(
    ...,
    concurrency=5,
    fail_fast=True,
    soft_concurrency_cap=None,
    hard_concurrency_cap=10,
    progress=False,
)
sql.airflow_query_label(...)
sql.set_time_print_sink("logging")
sql.get_sql_connection(...)
sql.with_sql_connection(...)

# Backend-specific helpers
sql.gp_create_many_partitions(...)
sql.build_gp_create_many_partitions_sqls(...)
sql.gp_vacuum(...)
sql.gp_cancel_all_running_queries(...)
sql.ch_create_table_as(...)
sql.ch_drop_table(...)
sql.ch_full_table_move(...)
```

[SQL module index](index.md)
