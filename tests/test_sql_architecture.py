from __future__ import annotations

import importlib
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SQL_ROOT = PROJECT_ROOT / "analytics_toolkit" / "sql"


def test_sql_facade_is_the_supported_public_surface() -> None:
    from analytics_toolkit import sql

    public_names = {
        "build_create_table_sql",
        "build_create_table_sqls",
        "build_gp_create_many_partitions_sqls",
        "create_sql_table",
        "drop_many_partitions",
        "execute_sql",
        "gp_create_many_partitions",
        "load_df",
        "read_sql",
        "show_tables",
        "table_info",
        "transfer_table",
    }

    for name in public_names:
        assert name in sql.__all__
        assert callable(getattr(sql, name))


@pytest.mark.parametrize(
    "module_name",
    [
        "analytics_toolkit.sql.async_api",
        "analytics_toolkit.sql.capabilities",
        "analytics_toolkit.sql.ch_lifecycle",
        "analytics_toolkit.sql.ch_options",
        "analytics_toolkit.sql.ch_wait",
        "analytics_toolkit.sql.identifiers",
        "analytics_toolkit.sql.labels",
        "analytics_toolkit.sql.operation_runner",
        "analytics_toolkit.sql.plan_steps",
        "analytics_toolkit.sql.plans",
        "analytics_toolkit.sql.query_timing",
        "analytics_toolkit.sql.show_tables",
        "analytics_toolkit.sql.table_info",
        "analytics_toolkit.sql.types",
        "analytics_toolkit.sql.ddl.create_sql_table",
        "analytics_toolkit.sql.dml.table.table_ops",
    ],
)
def test_removed_sql_deep_imports_fail_with_facade_guidance(module_name: str) -> None:
    with pytest.raises(ImportError) as exc_info:
        importlib.import_module(module_name)

    message = str(exc_info.value)
    assert module_name in message
    assert "from analytics_toolkit import sql" in message
    assert "import analytics_toolkit.sql as sql" in message


def test_sql_docs_state_facade_import_policy() -> None:
    docs = [
        PROJECT_ROOT / "README.md",
        SQL_ROOT / "README.md",
        PROJECT_ROOT / "manuals" / "ANALYTICS_TOOLKIT_MANUAL.md",
        PROJECT_ROOT / "manuals" / "AIRFLOW_SQL_MANUAL.md",
        PROJECT_ROOT / "analytics_toolkit" / "ab_utils" / "README.md",
    ]

    for path in docs:
        text = path.read_text()
        assert "from analytics_toolkit import sql" in text

    for path in docs[:-1]:
        text = path.read_text()
        assert "Deep imports under" in text
        assert "Do not restore removed root implementation paths" in text


def test_sql_source_does_not_restore_removed_aggregation_imports() -> None:
    forbidden_imports = [
        "analytics_toolkit.sql.ddl.create_sql_table",
        "analytics_toolkit.sql.dml.table.table_ops",
        "from ...ddl.create_sql_table",
        "from ..ddl.create_sql_table",
        "from .ddl.create_sql_table",
        "from ...dml.table.table_ops",
        "from ..dml.table.table_ops",
        "from .dml.table.table_ops",
        "from .table_ops",
    ]
    allowed_paths = {
        SQL_ROOT / "ddl" / "create_sql_table.py",
        SQL_ROOT / "dml" / "table" / "table_ops.py",
    }

    offenders: list[str] = []
    for path in SQL_ROOT.rglob("*.py"):
        if path in allowed_paths:
            continue
        text = path.read_text()
        for forbidden in forbidden_imports:
            if forbidden in text:
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}: {forbidden}")

    assert offenders == []


def test_sql_modules_stay_below_architecture_size_threshold() -> None:
    max_lines = 900
    allowed_large_modules = {
        SQL_ROOT / "connection" / "config.py",
        SQL_ROOT / "dml" / "load" / "load_df.py",
        SQL_ROOT / "dml" / "table" / "ch_full_table_move.py",
    }

    oversized: list[str] = []
    for path in SQL_ROOT.rglob("*.py"):
        if path in allowed_large_modules:
            continue
        line_count = len(path.read_text().splitlines())
        if line_count > max_lines:
            oversized.append(
                f"{path.relative_to(PROJECT_ROOT)} has {line_count} lines"
            )

    assert oversized == []
