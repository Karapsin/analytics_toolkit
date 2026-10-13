from __future__ import annotations

import ast

from tests._support.source import contains_expression, imported_modules, referenced_names
from tests.sql._support.architecture import (
    PROJECT_ROOT,
    SQL_ROOT,
)


def test_create_table_clickhouse_option_policy_is_adapter_owned() -> None:
    branch_checked_paths = [
        SQL_ROOT / "ddl" / "api.py",
        SQL_ROOT / "dml" / "load" / "load_df.py",
        SQL_ROOT / "dml" / "table" / "create_table_from_sql.py",
        SQL_ROOT / "dml" / "transfer" / "flow" / "attempt.py",
        SQL_ROOT / "dml" / "transfer" / "flow" / "api.py",
        SQL_ROOT / "dml" / "transfer" / "flow" / "stage.py",
    ]
    generic_callers = branch_checked_paths
    branch_snippets = {
        'target_backend == "ch"',
        'target_backend == "gp"',
        'backend != "ch"',
    }
    offenders: list[str] = []
    for path in branch_checked_paths:
        tree = ast.parse(path.read_text())
        for snippet in branch_snippets:
            if contains_expression(tree, snippet):
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}: {snippet}")
    for path in generic_callers:
        tree = ast.parse(path.read_text())
        if "validate_ch_options_not_used" in referenced_names(tree):
            offenders.append(f"{path.relative_to(PROJECT_ROOT)}: validate_ch_options_not_used")
        if any(
            module.startswith("analytics_toolkit.sql.clickhouse.options")
            for module in imported_modules(path)
        ):
            offenders.append(f"{path.relative_to(PROJECT_ROOT)}: clickhouse.options")

    assert offenders == []
