"""Fresh task specifications shared by async and threaded orchestration tests."""

from __future__ import annotations

from typing import Any

import pandas as pd


def named_tasks(tasks: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"name": name, **spec} for name, spec in tasks.items()]


def sql_task_spec(task_type: str) -> dict[str, Any]:
    if task_type == "read":
        return {"type": "read", "db_key": "gp", "query": "select 1"}
    if task_type == "execute":
        return {"type": "execute", "db_key": "gp", "query": "select 1"}
    if task_type == "execute_read":
        return {"type": "execute_read", "db_key": "gp", "query": "select 1"}
    if task_type == "load_df":
        return {
            "type": "load_df",
            "db_key": "gp",
            "destination_table": "sandbox.target",
            "df": pd.DataFrame({"id": [1]}),
        }
    if task_type == "transfer":
        return {
            "type": "transfer",
            "from_db": "gp",
            "to_db": "trino",
            "from_sql": "select 1",
            "to_table": "sandbox.target",
        }
    message = f"Unsupported task type for test: {task_type}"
    raise ValueError(message)
