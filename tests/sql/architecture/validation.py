from __future__ import annotations

import ast

from tests._support.source import contains_expression
from tests.sql._support.architecture import (
    PROJECT_ROOT,
    SQL_ROOT,
)


def test_trino_insert_chunk_size_validation_is_adapter_owned() -> None:
    checked_paths = [
        SQL_ROOT / "dml" / "load" / "load_df.py",
        SQL_ROOT / "dml" / "table" / "create_table_from_sql.py",
        SQL_ROOT / "dml" / "transfer" / "flow" / "api.py",
    ]
    offenders = []
    for path in checked_paths:
        tree = ast.parse(path.read_text())
        invalid_chunk_check = contains_expression(tree, "trino_insert_chunk_size <= 0")
        guarded_validation = any(
            isinstance(node, ast.BoolOp)
            and isinstance(node.op, ast.And)
            and any(
                contains_expression(value, "trino_insert_chunk_size is not None")
                for value in node.values
            )
            for node in ast.walk(tree)
        )
        if invalid_chunk_check or guarded_validation:
            offenders.append(str(path.relative_to(PROJECT_ROOT)))
    assert offenders == []
