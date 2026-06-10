from __future__ import annotations

import inspect
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SQL_ROOT = PROJECT_ROOT / "analytics_toolkit" / "sql"


def test_sql_facade_is_the_supported_public_surface() -> None:
    from analytics_toolkit import sql

    public_names = {
        "create_sql_table",
        "drop_many_partitions",
        "execute",
        "generate_dummy_connections",
        "gp_create_many_partitions",
        "load_df",
        "read",
        "show_tables",
        "table_info",
        "transfer",
    }

    for name in public_names:
        assert name in sql.__all__
        assert callable(getattr(sql, name))

    removed_public_aliases = {
        "build_create_table_sql",
        "build_create_table_sqls",
        "build_gp_create_many_partitions_sqls",
        "ch_full_table_move",
        "create_table_from_sql",
        "execute_sql",
        "read_sql",
        "transfer_table",
    }
    for name in removed_public_aliases:
        assert name not in sql.__all__
        assert not hasattr(sql, name)


def test_sql_public_operations_do_not_expose_backend_or_connection_inputs() -> None:
    from analytics_toolkit import sql

    forbidden_params = {"connection", "connection_type", "connection_key", "backend"}
    allowlist = {
        "format_plan",
        "generate_dummy_connections",
        "get_time_print_sink",
        "set_time_print_sink",
        "time_print",
        "validate_connections",
    }
    offenders: list[str] = []
    for name in sql.__all__:
        if name in allowlist:
            continue
        exported = getattr(sql, name)
        if not inspect.isfunction(exported):
            continue
        params = set(inspect.signature(exported).parameters)
        exposed = sorted(params & forbidden_params)
        if exposed:
            offenders.append(f"{name}: {', '.join(exposed)}")

    assert offenders == []


def test_single_db_sql_public_operations_use_db_key() -> None:
    from analytics_toolkit import sql

    single_db_operations = {
        "ch_drop_table",
        "create_sql_table",
        "drop_many_partitions",
        "execute",
        "execute_read",
        "extract_ddl",
        "gp_cancel_all_running_queries",
        "gp_create_many_partitions",
        "gp_vacuum",
        "load_df",
        "read",
        "show_tables",
        "table_info",
    }
    missing = [
        name
        for name in sorted(single_db_operations)
        if "db_key" not in inspect.signature(getattr(sql, name)).parameters
    ]

    assert missing == []


def test_removed_sql_deep_module_files_stay_removed() -> None:
    removed_paths = [
        SQL_ROOT / "async_api.py",
        SQL_ROOT / "capabilities.py",
        SQL_ROOT / "ch_lifecycle.py",
        SQL_ROOT / "ch_options.py",
        SQL_ROOT / "ch_wait.py",
        SQL_ROOT / "identifiers.py",
        SQL_ROOT / "labels.py",
        SQL_ROOT / "operation_runner.py",
        SQL_ROOT / "plan_steps.py",
        SQL_ROOT / "plans.py",
        SQL_ROOT / "query_timing.py",
        SQL_ROOT / "show_tables.py",
        SQL_ROOT / "table_info.py",
        SQL_ROOT / "types.py",
        SQL_ROOT / "ddl" / "create_sql_table.py",
        SQL_ROOT / "dml" / "table" / "ch_full_table_move.py",
        SQL_ROOT / "dml" / "table" / "table_ops.py",
        SQL_ROOT / "orchestration" / "async_api.py",
    ]

    assert [
        path.relative_to(PROJECT_ROOT) for path in removed_paths if path.exists()
    ] == []


def test_sql_docs_state_facade_import_policy() -> None:
    facade_import_docs = [
        PROJECT_ROOT / "README.md",
        PROJECT_ROOT / "docs" / "modules" / "sql" / "functions" / "index.md",
        PROJECT_ROOT / "docs" / "AIRFLOW_SQL_MANUAL.md",
        PROJECT_ROOT / "docs" / "modules" / "ab_utils" / "index.md",
    ]
    deep_import_policy_docs = [
        PROJECT_ROOT / "docs" / "modules" / "sql" / "functions" / "index.md",
        PROJECT_ROOT / "docs" / "AIRFLOW_SQL_MANUAL.md",
    ]

    assert not (PROJECT_ROOT / "docs" / "ANALYTICS_TOOLKIT_MANUAL.md").exists()

    for path in facade_import_docs:
        text = path.read_text()
        assert "from analytics_toolkit import sql" in text

    for path in deep_import_policy_docs:
        text = path.read_text()
        assert "Deep imports under" in text

    agents = (PROJECT_ROOT / "AGENTS.md").read_text()
    assert "Do not restore removed root implementation paths" in agents


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
        "orchestration.async_api",
        "from .async_api",
    ]
    offenders: list[str] = []
    for path in SQL_ROOT.rglob("*.py"):
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
