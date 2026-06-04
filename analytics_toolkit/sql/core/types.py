from __future__ import annotations

from typing import Literal

BackendName = Literal["gp", "trino", "ch"]
ConnectionKey = str
SqlText = str
TableName = str
SqlTaskType = Literal[
    "read",
    "execute",
    "execute_read",
    "load_df",
    "transfer",
    "custom_sql_pipeline",
]
