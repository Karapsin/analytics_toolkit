[SQL functions index](index.md)

# create_sql_table

Deprecated compatibility alias for [sql.create_table](create_table.md). It has
the same signature and emits `DeprecationWarning` on every call. Use
`sql.create_table` in new code.

```python
create_sql_table(db_key: 'str', table_name: 'str', df: 'pd.DataFrame | None' = None, *, sql: 'str | None' = None, source_db: 'str | None' = None, insert_data: 'bool' = False, drop_if_exists: 'bool' = False, if_not_exists: 'bool' = False, gp_distributed_by_key: 'str | Sequence[str] | None' = None, gp_partitions: 'Mapping[str, Any] | None' = None, partition_by: 'Sequence[str] | str | None' = None, order_by: 'Sequence[str] | str | None' = None, ch_engine: 'str | None' = None, ch_cluster: 'str | None' = None, ch_sharding_key: 'str | None' = None, ch_distributed_table: 'bool | None' = None, ch_distributed_engine_template: 'str | None' = None, ch_distributed_cluster: 'str | None' = None, ch_shard_on_cluster: 'str | None' = None, ch_distributed_on_cluster: 'str | None' = None, ch_ddl_ready_timeout_seconds: 'float | None' = None, ch_ddl_wait_policy: 'str | None' = None, ch_only_shard: 'bool' = False, ch_replace_table: 'bool' = False, retry_cnt: 'int' = 5, timeout_increment: 'float' = 5, dry_run: 'bool' = False, return_sql: 'bool' = False, only_generate_sql: 'bool' = False, query_label: 'str | None' = None, return_metadata: 'bool' = False, table_schema: 'Mapping[str, str] | None' = None, drop_target_if_exists: 'bool | None' = None) -> 'str | SqlPlan | SqlOperationResult | int | None'
```

[SQL functions index](index.md)
