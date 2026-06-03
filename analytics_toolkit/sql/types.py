from __future__ import annotations

from typing import Literal, TypeAlias

BackendName: TypeAlias = Literal["gp", "trino", "ch"]
ConnectionKey: TypeAlias = str
SqlText: TypeAlias = str
TableName: TypeAlias = str
SqlTaskType: TypeAlias = Literal[
    "read",
    "execute",
    "execute_read",
    "load_df",
    "transfer",
    "custom_sql_pipeline",
]
