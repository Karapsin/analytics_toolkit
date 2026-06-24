from __future__ import annotations

import ast
import inspect
import importlib
import pkgutil
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SQL_ROOT = PROJECT_ROOT / "analytics_toolkit" / "sql"


def test_sql_facade_is_the_supported_public_surface() -> None:
    from analytics_toolkit import sql

    public_names = {
        "cancel_queries",
        "create_sql_table",
        "drop_partitions",
        "drop_tables",
        "execute",
        "generate_dummy_connections",
        "gp_create_partitions",
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
        "build_gp_create_partitions_sqls",
        "ch_full_table_move",
        "ch_drop_table",
        "create_table_from_sql",
        "execute_sql",
        "drop_paritions",
        "drop_many_partitions",
        "drop_table",
        "gp_cancel_all_running_queries",
        "gp_create_many_partitions",
        "read_sql",
        "transfer_table",
    }
    for name in removed_public_aliases:
        assert name not in sql.__all__
        assert not hasattr(sql, name)


def test_transfer_api_reload_tolerates_stale_transfer_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api_module = importlib.import_module(
        "analytics_toolkit.sql.dml.transfer.flow.api"
    )
    options_module = importlib.import_module(
        "analytics_toolkit.sql.dml.transfer.flow.options"
    )

    assert hasattr(options_module, "resolve_trino_mode")
    with monkeypatch.context() as patch:
        patch.delattr(options_module, "resolve_trino_mode")
        reloaded_api = importlib.reload(api_module)

    assert callable(reloaded_api.transfer_table)
    assert hasattr(options_module, "resolve_trino_mode")
    importlib.reload(api_module)


def test_sql_facade_reload_tolerates_stale_transfer_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sql_module = importlib.import_module("analytics_toolkit.sql")
    api_module = importlib.import_module(
        "analytics_toolkit.sql.dml.transfer.flow.api"
    )
    options_module = importlib.import_module(
        "analytics_toolkit.sql.dml.transfer.flow.options"
    )

    assert hasattr(options_module, "resolve_trino_mode")
    with monkeypatch.context() as patch:
        patch.delattr(options_module, "resolve_trino_mode")
        importlib.reload(api_module)
        reloaded_sql = importlib.reload(sql_module)

    assert callable(reloaded_sql.show_tables)
    assert callable(reloaded_sql.transfer)
    assert "show_tables" in reloaded_sql.__all__
    assert "transfer" in reloaded_sql.__all__
    assert hasattr(options_module, "resolve_trino_mode")
    importlib.reload(api_module)
    importlib.reload(sql_module)


def test_sql_submodules_import_cleanly() -> None:
    import analytics_toolkit.sql as sql_package

    failed_imports: list[str] = []
    for module_info in pkgutil.walk_packages(
        sql_package.__path__,
        prefix=f"{sql_package.__name__}.",
    ):
        try:
            importlib.import_module(module_info.name)
        except Exception as exc:  # pragma: no cover - assertion reports details
            failed_imports.append(f"{module_info.name}: {type(exc).__name__}: {exc}")

    assert failed_imports == []


def test_sql_backend_registry_is_canonical_backend_list() -> None:
    from analytics_toolkit.sql.backends import BACKEND_REGISTRY, get_backend_names
    from analytics_toolkit.sql.connection.config import SUPPORTED_BACKENDS
    from analytics_toolkit.sql.core.capabilities import BACKEND_CAPABILITIES

    backend_names = set(get_backend_names())
    assert backend_names == set(BACKEND_REGISTRY)
    assert SUPPORTED_BACKENDS == backend_names
    assert set(BACKEND_CAPABILITIES) == backend_names
    assert {
        backend_name: backend.capability
        for backend_name, backend in BACKEND_REGISTRY.items()
    } == BACKEND_CAPABILITIES


def test_sql_backend_compatibility_shims_do_not_own_implementations() -> None:
    shim_paths = {
        SQL_ROOT / "backend_adapters.py",
        *(SQL_ROOT / "_backend_adapters").glob("*.py"),
    }
    offenders: list[str] = []
    for path in SQL_ROOT.rglob("*.py"):
        if path in shim_paths:
            continue
        text = path.read_text()
        if "._backend_adapters" in text or "sql._backend_adapters" in text:
            offenders.append(str(path.relative_to(PROJECT_ROOT)))

    assert offenders == []


def test_generic_sql_backend_branch_debt_does_not_increase() -> None:
    allowed_counts = {
        "analytics_toolkit/sql/clickhouse/options.py": 2,
        "analytics_toolkit/sql/ddl/api.py": 4,
        "analytics_toolkit/sql/ddl/builders.py": 1,
        "analytics_toolkit/sql/ddl/extract_ddl.py": 3,
        "analytics_toolkit/sql/dml/io/execute_sql.py": 0,
        "analytics_toolkit/sql/dml/load/load_df.py": 5,
        "analytics_toolkit/sql/dml/load/load_sql_table.py": 0,
        "analytics_toolkit/sql/dml/table/ch_create_table_as.py": 0,
        "analytics_toolkit/sql/dml/table/create_table_from_sql.py": 5,
        "analytics_toolkit/sql/dml/table/drop_tables.py": 0,
        "analytics_toolkit/sql/dml/table/maintenance.py": 1,
        "analytics_toolkit/sql/dml/table/partitions.py": 1,
        "analytics_toolkit/sql/dml/table/write_modes.py": 0,
        "analytics_toolkit/sql/dml/transfer/flow/api.py": 6,
        "analytics_toolkit/sql/dml/transfer/flow/attempt.py": 1,
        "analytics_toolkit/sql/dml/transfer/flow/estimate.py": 0,
        "analytics_toolkit/sql/dml/transfer/flow/finalize.py": 0,
        "analytics_toolkit/sql/dml/transfer/flow/options.py": 2,
        "analytics_toolkit/sql/dml/transfer/flow/stage.py": 1,
        "analytics_toolkit/sql/dml/transfer/io/source.py": 0,
        "analytics_toolkit/sql/dml/transfer/staging.py": 0,
        "analytics_toolkit/sql/execution/operation_runner.py": 0,
        "analytics_toolkit/sql/execution/plan_steps.py": 0,
        "analytics_toolkit/sql/metadata/show_tables.py": 4,
        "analytics_toolkit/sql/metadata/table_info.py": 1,
    }
    needles = (
        'backend == "',
        'backend != "',
        'backend in {"',
        "config.backend ==",
        "config.backend !=",
        "options.backend ==",
        "options.backend !=",
        "options.connection_backend ==",
        "options.connection_backend !=",
        "target_config.backend ==",
        "target_config.backend !=",
        "source_config.backend ==",
        "source_config.backend !=",
        "connection_backend ==",
        "connection_backend !=",
        "connection_backend in {",
        "target_backend ==",
        "target_backend !=",
        "from_db_backend ==",
        "from_db_backend !=",
        "to_db_backend ==",
        "to_db_backend !=",
        "transfer_backend ==",
        "transfer_backend !=",
    )
    excluded_paths = {
        SQL_ROOT / "backend_adapters.py",
    }
    excluded_parts = {"backends", "_backend_adapters"}
    offenders: list[str] = []

    for path in SQL_ROOT.rglob("*.py"):
        relative = str(path.relative_to(PROJECT_ROOT))
        if path in excluded_paths or any(part in excluded_parts for part in path.parts):
            continue
        count = sum(
            1
            for line in path.read_text().splitlines()
            if any(needle in line for needle in needles)
        )
        if count > allowed_counts.get(relative, 0):
            offenders.append(
                f"{relative}: {count} backend branches "
                f"(allowed {allowed_counts.get(relative, 0)})"
            )

    assert offenders == []


def test_generic_sql_modules_do_not_pin_literal_backend_sets() -> None:
    literal_sets = {
        '{"gp", "trino", "ch"}',
        '{"trino", "gp", "ch"}',
        '{"gp","trino","ch"}',
        '{"trino","gp","ch"}',
    }
    excluded_paths = {
        SQL_ROOT / "backend_adapters.py",
    }
    excluded_parts = {"backends", "_backend_adapters"}
    offenders: list[str] = []

    for path in SQL_ROOT.rglob("*.py"):
        relative = str(path.relative_to(PROJECT_ROOT))
        if path in excluded_paths or any(part in excluded_parts for part in path.parts):
            continue
        text = path.read_text()
        if any(literal_set in text for literal_set in literal_sets):
            offenders.append(relative)

    assert offenders == []


def test_adapter_owned_sql_fragments_stay_out_of_cleaned_generic_modules() -> None:
    cleaned_generic_paths = [
        SQL_ROOT / "metadata" / "show_tables.py",
        SQL_ROOT / "ddl" / "extract_ddl.py",
        SQL_ROOT / "dml" / "table" / "partitions.py",
        SQL_ROOT / "dml" / "transfer" / "staging.py",
    ]
    forbidden_snippets = {
        "system.tables",
        "information_schema.tables",
        "pg_catalog",
        "DESCRIBE TABLE (",
        "DROP PARTITION",
        "TRUNCATE PARTITION",
        "ADD PARTITION",
        "DELETE FROM {table}",
    }
    offenders: list[str] = []

    for path in cleaned_generic_paths:
        text = path.read_text()
        for snippet in forbidden_snippets:
            if snippet in text:
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}: {snippet}")

    assert offenders == []


def test_clickhouse_compatibility_wrappers_do_not_own_sql() -> None:
    wrapper_paths = [
        SQL_ROOT / "ddl" / "clickhouse.py",
        SQL_ROOT / "clickhouse" / "lifecycle.py",
        SQL_ROOT / "clickhouse" / "wait.py",
        SQL_ROOT / "dml" / "table" / "ch_create_table_as.py",
        SQL_ROOT / "dml" / "transfer" / "flow" / "estimate.py",
    ]
    forbidden_snippets = {
        "CREATE TABLE",
        "DROP TABLE",
        "TRUNCATE TABLE",
        "INSERT INTO",
        "EXPLAIN",
        "VACUUM",
        "ENGINE =",
        "MergeTree",
        "Distributed(",
        "ON CLUSTER",
        "system.",
    }
    offenders: list[str] = []

    for path in wrapper_paths:
        text = path.read_text()
        for snippet in forbidden_snippets:
            if snippet in text:
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}: {snippet}")

    assert offenders == []


def test_generic_clickhouse_callers_delegate_to_adapters() -> None:
    generic_paths = [
        SQL_ROOT / "dml" / "table" / "drop_tables.py",
        SQL_ROOT / "execution" / "plan_steps.py",
        SQL_ROOT / "metadata" / "table_info.py",
    ]
    forbidden_snippets = {
        "clickhouse.lifecycle",
        "clickhouse.wait",
        "ddl.clickhouse",
        "build_ch_",
        "drop_ch_",
        "truncate_ch_",
        "get_ch_connection_for_host",
        'backend == "ch"',
        'backend != "ch"',
        'config.backend == "ch"',
        'options.backend == "ch"',
    }
    offenders: list[str] = []

    for path in generic_paths:
        text = path.read_text()
        for snippet in forbidden_snippets:
            if snippet in text:
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}: {snippet}")

    assert offenders == []


def test_clickhouse_backend_modules_do_not_import_legacy_wait_shim() -> None:
    offenders: list[str] = []
    for path in (SQL_ROOT / "backends" / "ch").rglob("*.py"):
        text = path.read_text()
        if "clickhouse.wait" in text:
            offenders.append(str(path.relative_to(PROJECT_ROOT)))
    if "clickhouse.wait" in (SQL_ROOT / "ddl" / "api.py").read_text():
        offenders.append("analytics_toolkit/sql/ddl/api.py")

    assert offenders == []


def test_trino_parquet_stage_sql_is_adapter_owned() -> None:
    generic_path = SQL_ROOT / "dml" / "transfer" / "flow" / "parquet_stage.py"
    text = generic_path.read_text()
    forbidden_snippets = {
        "format = 'PARQUET'",
        "external_location = '",
        "parse_one",
        "sqlglot",
        "Decimal",
        "datetime",
        "_infer_trino_type_from_values",
    }
    offenders = [
        f"{generic_path.relative_to(PROJECT_ROOT)}: {snippet}"
        for snippet in forbidden_snippets
        if snippet in text
    ]
    assert offenders == []

    tree = ast.parse(text, filename=str(generic_path))
    wrapper_methods = {
        "build_create_parquet_stage_table_sql": "build_parquet_stage_table_sql",
        "build_stage_external_location": "parquet_stage_target_table_base",
        "infer_trino_column_types_from_rows": (
            "infer_parquet_stage_column_types_from_rows"
        ),
    }
    for wrapper_name, adapter_method in wrapper_methods.items():
        wrapper = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == wrapper_name
        )
        attribute_calls = {
            node.func.attr
            for node in ast.walk(wrapper)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert adapter_method in attribute_calls


def test_dataframe_type_inference_is_adapter_owned() -> None:
    schema_path = SQL_ROOT / "ddl" / "schema.py"
    text = schema_path.read_text()
    forbidden_snippets = {
        "def _infer_gp_type",
        "def _infer_trino_type",
        "def _infer_ch_type",
        "_COLUMN_TYPE_INFERERS",
        "isinstance(value, Decimal)",
        "DOUBLE PRECISION",
        "DateTime64(6)",
        "Nullable(",
    }
    offenders = [
        f"{schema_path.relative_to(PROJECT_ROOT)}: {snippet}"
        for snippet in forbidden_snippets
        if snippet in text
    ]
    assert offenders == []

    tree = ast.parse(text, filename=str(schema_path))
    infer_wrapper = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_infer_backend_type"
    )
    attribute_calls = {
        node.func.attr
        for node in ast.walk(infer_wrapper)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "infer_dataframe_column_type" in attribute_calls


def test_load_insert_backend_serialization_is_adapter_owned() -> None:
    load_path = SQL_ROOT / "dml" / "load" / "load_sql_table.py"
    text = load_path.read_text()
    forbidden_snippets = {
        "psycopg2.extras",
        "from decimal import Decimal",
        "TrinoConfig",
        "SqlConfigError",
        "isinstance(value, Decimal)",
        "normalized_target_type",
        "to_pydatetime",
        "applymap(_normalize_ch_scalar)",
    }
    offenders = [
        f"{load_path.relative_to(PROJECT_ROOT)}: {snippet}"
        for snippet in forbidden_snippets
        if snippet in text
    ]
    assert offenders == []


def test_table_info_name_resolution_is_adapter_owned() -> None:
    table_info_path = SQL_ROOT / "metadata" / "table_info.py"
    text = table_info_path.read_text()
    forbidden_snippets = {
        "split_trino_table_name",
        'backend != "trino"',
        'backend == "trino"',
    }
    offenders = [
        f"{table_info_path.relative_to(PROJECT_ROOT)}: {snippet}"
        for snippet in forbidden_snippets
        if snippet in text
    ]
    assert offenders == []
    assert "resolve_table_info_table_name" in text


def test_upsert_backend_policy_is_capability_owned() -> None:
    assert not (SQL_ROOT / "dml" / "table" / "upsert_policy.py").exists()

    forbidden_snippets = {
        "is_trino_backend",
        "is_clickhouse_backend",
        "PARTITION_REPLACEMENT_UPSERT_BACKENDS",
    }
    offenders: list[str] = []
    for path in SQL_ROOT.rglob("*.py"):
        if "backends" in path.parts:
            continue
        text = path.read_text()
        for snippet in forbidden_snippets:
            if snippet in text:
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}: {snippet}")

    assert offenders == []


def test_backend_specific_helper_bodies_stay_backend_owned() -> None:
    allowed_helper_defs = {
        "analytics_toolkit/sql/dml/io/execute_read.py": {
            "_execute_read_ch",
            "_execute_read_ch_backend",
            "_execute_read_gp",
            "_execute_read_gp_backend",
            "_execute_read_trino",
            "_execute_read_trino_backend",
        },
        "analytics_toolkit/sql/dml/io/execute_sql.py": {
            "_execute_ch",
            "_execute_ch_backend",
            "_execute_ch_statement",
            "_execute_gp",
            "_execute_gp_backend",
            "_execute_trino",
            "_execute_trino_backend",
            "_execute_trino_statement",
        },
        "analytics_toolkit/sql/dml/io/read_sql.py": {
            "_read_ch",
            "_read_ch_backend",
            "_read_gp",
            "_read_gp_backend",
            "_read_trino",
            "_read_trino_backend",
        },
        "analytics_toolkit/sql/dml/load/load_sql_table.py": {
            "_build_trino_values_tuple",
            "_chunk_rows",
            "_chunk_sequence",
            "_get_gp_insert_chunk_size",
            "_get_trino_insert_chunk_size",
            "_insert_ch_batch",
            "_insert_ch_batch_backend",
            "_insert_ch_rows",
            "_insert_ch_rows_backend",
            "_insert_gp_batch",
            "_insert_gp_batch_backend",
            "_insert_gp_rows",
            "_insert_gp_rows_backend",
            "_insert_trino_batch",
            "_insert_trino_batch_backend",
            "_insert_trino_rows",
            "_insert_trino_rows_backend",
            "_iter_trino_row_values",
            "_iter_trino_rows",
            "_normalize_ch_row",
            "_normalize_ch_scalar",
            "_normalize_trino_value",
            "_trino_literal",
            "build_gp_batch_insert_sql",
            "build_trino_batch_insert_sql",
            "normalize_ch_batch",
        },
        "analytics_toolkit/sql/dml/table/write_modes.py": {
            "_build_ch_delete_matching_stage_sql",
            "_build_ch_normalized_key_tuple",
            "_build_gp_delete_matching_stage_sql",
            "_build_trino_merge_placeholder_sql",
            "_build_trino_merge_sql",
            "_ensure_ch_distributed_target_pair",
        },
    }
    backend_tokens = ("_ch", "_gp", "_trino")
    offenders: list[str] = []

    for relative in allowed_helper_defs:
        path = PROJECT_ROOT / relative
        relative = str(path.relative_to(PROJECT_ROOT))
        allowed = allowed_helper_defs.get(relative, set())
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            if not any(token in node.name for token in backend_tokens):
                continue
            if node.name not in allowed:
                offenders.append(f"{relative}: {node.name}")

    assert offenders == []


def test_backend_adapters_do_not_depend_on_generic_write_mode_or_type_maps() -> None:
    forbidden_snippets = {
        "dml.transfer.schema",
        "dml.transfer import schema",
        "dml.load import load_sql_table",
        "dml.load.load_sql_table",
        "load_sql_table.",
        "from ...dml.table import write_modes",
        "transfer_schema._map_to_gp_type",
        "transfer_schema._map_to_trino_type",
        "transfer_schema._map_to_ch_base_type",
        "transfer.schema import _GP_OID_TYPES",
    }
    offenders: list[str] = []

    for path in (SQL_ROOT / "backends").rglob("*.py"):
        text = path.read_text()
        for snippet in forbidden_snippets:
            if snippet in text:
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}: {snippet}")

    assert offenders == []


def test_transfer_schema_backend_compatibility_wrapper_delegates_to_adapter() -> None:
    schema_path = SQL_ROOT / "dml" / "transfer" / "schema.py"
    tree = ast.parse(schema_path.read_text(), filename=str(schema_path))
    wrapper = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "refine_ch_column_types_nullability_from_rows"
    )

    call_names = {
        node.func.id
        for node in ast.walk(wrapper)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert call_names == {"refine_stage_column_types_from_rows"}


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
        "create_sql_table",
        "cancel_queries",
        "drop_partitions",
        "drop_tables",
        "execute",
        "execute_read",
        "extract_ddl",
        "gp_create_partitions",
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
