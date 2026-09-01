from __future__ import annotations

import warnings
from functools import wraps
from typing import Any

from .api import create_table


@wraps(create_table)
def create_sql_table(*args: Any, **kwargs: Any) -> Any:
    warnings.warn(
        "sql.create_sql_table is deprecated; use sql.create_table instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    return create_table(*args, **kwargs)


create_sql_table.__name__ = "create_sql_table"
create_sql_table.__qualname__ = "create_sql_table"

__all__ = ["create_sql_table"]
