from __future__ import annotations

import importlib
import inspect
import threading
import time
from typing import Any

import pandas as pd
import pytest
from analytics_toolkit.general import time_print

parallel_module = importlib.import_module("analytics_toolkit.sql.orchestration.parallel_sql")

tasks_module = importlib.import_module("analytics_toolkit.sql.orchestration.tasks")

sql_module = importlib.import_module("analytics_toolkit.sql")


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
    raise ValueError(f"Unsupported task type for test: {task_type}")


__all__ = [
    "Any",
    "importlib",
    "inspect",
    "named_tasks",
    "parallel_module",
    "pd",
    "pytest",
    "sql_module",
    "sql_task_spec",
    "tasks_module",
    "threading",
    "time",
    "time_print",
]
