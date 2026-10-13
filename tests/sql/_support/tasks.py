"""Fresh task specifications shared by async and threaded orchestration tests."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

import pandas as pd

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Literal

    import pytest


def patch_task_operations(
    monkeypatch: pytest.MonkeyPatch,
    *,
    mode: Literal["async", "parallel"],
    **operations: Callable[..., Any],
) -> None:
    """Keep orchestration's operation patch locations in one compatibility seam."""
    module_name = {"async": "async_sql", "parallel": "parallel_sql"}[mode]
    module = importlib.import_module(f"analytics_toolkit.sql.orchestration.{module_name}")
    targets = {
        "read": "read_sql",
        "execute": "execute_sql",
        "execute_read": "execute_read",
        "load_df": "load_df",
        "transfer": "transfer_table",
    }
    unknown = set(operations) - set(targets)
    if unknown:
        message = f"Unsupported fake task operations: {sorted(unknown)}"
        raise ValueError(message)
    for operation, runner in operations.items():
        monkeypatch.setattr(module, targets[operation], runner)


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
