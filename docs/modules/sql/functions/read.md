[SQL functions index](index.md)

# read

Run one SQL query through a configured connection and return a pandas dataframe.

```python
read(connection_type: 'str', query: 'str', print_queries: 'bool' = False, retry_cnt: 'int' = 5, timeout_increment: 'int | float' = 5, query_label: 'str | None' = None, return_metadata: 'bool' = False) -> 'pd.DataFrame | SqlOperationResult'
```

## Inputs

- `connection_type`: Connection key or alias from `.connections`; backend dispatch is selected from that entry.
- `query`: SQL text to execute or read.
- `print_queries`: Whether to print SQL text before execution.
- `retry_cnt`: Number of operation retries with fresh connections.
- `timeout_increment`: Delay increment used between operation retries.
- `query_label`: Safe label added to generated SQL comments, plans, metadata, and logs.
- `return_metadata`: When `True`, return `SqlOperationResult` instead of the historical bare value.

## Notes

- Prefer this short entrypoint in user-facing examples.

[SQL functions index](index.md)
