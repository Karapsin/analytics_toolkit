[SQL functions index](index.md)

# read

Run one SQL query through a configured connection and return the selected output shape.

```python
read(db_key: 'str', query: 'str', print_queries: 'bool' = False, retry_cnt: 'int' = 5, timeout_increment: 'int | float' = 5, query_label: 'str | None' = None, return_metadata: 'bool' = False, output_type: 'ReadOutputType' = 'df', to_excel: 'str | None' = None) -> 'Any | SqlOperationResult'
```

## Inputs

- `db_key` - connection key or alias from `.connections`; backend dispatch is selected from that entry
- `query` - text of SQL to execute or read
- `output_type` - output shape: `df`, `scalar`, `list`, or `dict`; defaults to `df`
- `to_excel` - optional `.xlsx` output filename; writes the dataframe without its index and requires `output_type="df"`
- `retry_cnt` - number of operation retries with fresh connections
- `timeout_increment` - delay increment used between operation retries
- `return_metadata` - when `True`, return `SqlOperationResult` instead of the historical bare value
- `print_queries` - whether to print SQL text before execution
- `query_label` - safe label added to generated SQL comments, plans, metadata, and logs

## Usage

```python
from analytics_toolkit import sql

orders = sql.read(
    db_key="gp",
    query="select order_id, user_id, amount from sandbox.orders limit 100",
)
```

Write the dataframe to Excel while keeping it as the return value:

```python
orders = sql.read(
    db_key="gp",
    query="select order_id, user_id, amount from sandbox.orders limit 100",
    to_excel="orders.xlsx",
)
```

Output example:

```python
orders.head()
#    order_id  user_id  amount
# 0      1001       42   19.90
# 1      1002       51   35.00
```

Return a single value only when the query has exactly one row and one column:

```python
order_count = sql.read(
    db_key="gp",
    query="select count(*) from sandbox.orders",
    output_type="scalar",
)

order_count
# 2
```

Use `list` for a one-column result, or `dict` for column-oriented lists:

```python
order_ids = sql.read(
    db_key="gp",
    query="select order_id from sandbox.orders order by order_id",
    output_type="list",
)
# [1001, 1002]

orders_by_column = sql.read(
    db_key="gp",
    query="select order_id, amount from sandbox.orders order by order_id",
    output_type="dict",
)
# {"order_id": [1001, 1002], "amount": [19.90, 35.00]}
```

## Notes

- Prefer this short entrypoint in user-facing examples.
- `scalar` requires exactly one row and one column; `list` requires exactly one column.
- `dict` reads backend columns directly without creating an intermediate dataframe and requires unique column names.
- With `return_metadata=True`, `SqlOperationResult.data` contains the selected output shape while row metadata retains the query row count.

[SQL functions index](index.md)
