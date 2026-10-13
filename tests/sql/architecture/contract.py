from __future__ import annotations

from tests._support.source import (
    CodeRules,
    backend_branch_count,
    called_names,
    contains_expression,
    forbidden_code,
    imported_modules,
    referenced_names,
)
from tests.sql._support.architecture import (
    PROJECT_ROOT,
    SQL_ROOT,
    ast,
    inspect,
)


def test_adapter_owned_sql_fragments_stay_out_of_cleaned_generic_modules() -> None:
    cleaned_generic_paths = [
        SQL_ROOT / "metadata" / "show_tables.py",
        SQL_ROOT / "ddl" / "extract_ddl.py",
        SQL_ROOT / "dml" / "table" / "partitions.py",
        SQL_ROOT / "dml" / "transfer" / "staging.py",
    ]
    offenders = [
        f"{path.relative_to(PROJECT_ROOT)}: {violation}"
        for path in cleaned_generic_paths
        for violation in forbidden_code(
            path,
            CodeRules(
                strings=(
                    "system.tables",
                    "information_schema.tables",
                    "pg_catalog",
                    "DESCRIBE TABLE (",
                    "DROP PARTITION",
                    "TRUNCATE PARTITION",
                    "ADD PARTITION",
                    "DELETE FROM ",
                )
            ),
        )
    ]
    assert offenders == []


def test_backend_adapters_do_not_depend_on_generic_write_mode_or_type_maps() -> None:
    offenders = [
        f"{path.relative_to(PROJECT_ROOT)}: {violation}"
        for path in (SQL_ROOT / "backends").rglob("*.py")
        for violation in forbidden_code(
            path,
            CodeRules(
                modules=(
                    "analytics_toolkit.sql.dml.transfer.schema",
                    "analytics_toolkit.sql.dml.load.load_sql_table",
                    "analytics_toolkit.sql.dml.table.write_modes",
                ),
                names=("load_sql_table",),
                expressions=(
                    "transfer_schema._map_to_gp_type",
                    "transfer_schema._map_to_trino_type",
                    "transfer_schema._map_to_ch_base_type",
                ),
            ),
        )
    ]
    assert offenders == []


def test_backend_policy_sql_templates_stay_backend_owned() -> None:
    offenders = [
        f"{path.relative_to(PROJECT_ROOT)}: {violation}"
        for path in (path for path in SQL_ROOT.rglob("*.py") if "backends" not in path.parts)
        for violation in forbidden_code(
            path, CodeRules(strings=("TRUNCATE TABLE IF EXISTS", "DROP PARTITION"))
        )
    ]
    assert offenders == []


def test_backend_specific_helper_bodies_stay_backend_owned() -> None:
    allowed_helper_defs = {
        "analytics_toolkit/sql/dml/io/execute_sql.py": {
            "_execute_ch_statement",
            "_execute_trino_statement",
        },
        "analytics_toolkit/sql/dml/load/load_sql_table.py": {
            "_build_trino_values_tuple",
            "_chunk_rows",
            "_chunk_sequence",
            "_get_gp_insert_chunk_size",
            "_get_trino_insert_chunk_size",
            "_insert_ch_batch",
            "_insert_ch_rows",
            "_insert_gp_batch",
            "_insert_gp_rows",
            "_insert_trino_batch",
            "_insert_trino_rows",
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


def test_dataframe_type_inference_is_adapter_owned() -> None:
    schema_path = SQL_ROOT / "ddl" / "schema.py"
    assert (
        forbidden_code(
            schema_path,
            CodeRules(
                names=("_build_expected_ch_column_types", "_COLUMN_TYPE_INFERERS"),
                prefixes=("_infer_gp_type", "_infer_trino_type", "_infer_ch_type"),
                expressions=("get_backend_adapter('ch')", "isinstance(value, Decimal)"),
                strings=("DOUBLE PRECISION", "DateTime64(6)", "Nullable("),
            ),
        )
        == []
    )
    tree = ast.parse(schema_path.read_text())
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


def test_generic_analyze_support_policy_is_adapter_owned() -> None:
    checked_paths = [
        SQL_ROOT / "execution" / "plan_steps.py",
        SQL_ROOT / "dml" / "table" / "maintenance.py",
    ]
    offenders = [
        f"{path.relative_to(PROJECT_ROOT)}: {violation}"
        for path in checked_paths
        for violation in forbidden_code(
            path, CodeRules(names=("get_backend_capability", "supports_analyze"))
        )
    ]
    assert offenders == []


def test_generic_clickhouse_callers_delegate_to_adapters() -> None:
    generic_paths = [
        SQL_ROOT / "dml" / "table" / "drop_tables.py",
        SQL_ROOT / "execution" / "plan_steps.py",
        SQL_ROOT / "metadata" / "table_info.py",
    ]
    offenders = [
        f"{path.relative_to(PROJECT_ROOT)}: {violation}"
        for path in generic_paths
        for violation in forbidden_code(
            path,
            CodeRules(
                modules=(
                    "analytics_toolkit.sql.clickhouse.lifecycle",
                    "analytics_toolkit.sql.clickhouse.wait",
                    "analytics_toolkit.sql.ddl.clickhouse",
                ),
                prefixes=("build_ch_", "drop_ch_", "truncate_ch_"),
                names=("get_ch_connection_for_host",),
                expressions=(
                    "backend == 'ch'",
                    "backend != 'ch'",
                    "config.backend == 'ch'",
                    "options.backend == 'ch'",
                ),
            ),
        )
    ]
    assert offenders == []


def test_generic_identifier_helpers_use_backend_adapters() -> None:
    helper_paths = {
        SQL_ROOT / "core" / "identifiers.py",
        SQL_ROOT / "ddl" / "identifiers.py",
    }
    for path in helper_paths:
        assert "get_backend_adapter" in called_names(ast.parse(path.read_text()))
        assert (
            forbidden_code(
                path,
                CodeRules(
                    names=("get_backend_capability", "BACKEND_CAPABILITIES"),
                    modules=(
                        "analytics_toolkit.sql.core.capabilities",
                        "analytics_toolkit.sql.capabilities",
                    ),
                ),
            )
            == []
        )


def test_generic_io_backend_dispatch_is_adapter_owned() -> None:
    checked_paths = [
        SQL_ROOT / "dml" / "io" / "read_sql.py",
        SQL_ROOT / "dml" / "io" / "execute_sql.py",
        SQL_ROOT / "dml" / "io" / "execute_read.py",
    ]
    offenders = [
        f"{path.relative_to(PROJECT_ROOT)}: {violation}"
        for path in checked_paths
        for violation in forbidden_code(
            path,
            CodeRules(
                names=(
                    "_READ_BACKENDS",
                    "_EXECUTE_BACKENDS",
                    "_EXECUTE_READ_BACKENDS",
                    "_execute_trino",
                    "_execute_gp",
                    "_execute_ch",
                ),
                prefixes=(
                    "_read_trino",
                    "_read_gp",
                    "_read_ch",
                    "_execute_read_trino",
                    "_execute_read_gp",
                    "_execute_read_ch",
                ),
                expressions=(
                    "get_backend_adapter('trino')",
                    "get_backend_adapter('gp')",
                    "get_backend_adapter('ch')",
                ),
            ),
        )
    ]
    assert offenders == []


def test_generic_load_and_transfer_do_not_import_concrete_config_policy() -> None:
    checked_paths = [
        SQL_ROOT / "dml" / "load" / "load_df.py",
        SQL_ROOT / "dml" / "transfer" / "flow" / "api.py",
        SQL_ROOT / "dml" / "transfer" / "flow" / "dry_run.py",
        SQL_ROOT / "dml" / "transfer" / "flow" / "finalize.py",
        SQL_ROOT / "dml" / "transfer" / "flow" / "stage.py",
    ]
    offenders = [
        f"{path.relative_to(PROJECT_ROOT)}: {violation}"
        for path in checked_paths
        for violation in forbidden_code(
            path,
            CodeRules(
                names=(
                    "TrinoConfig",
                    "get_backend_capability",
                    "upsert_strategy",
                    "supports_distributed_tables",
                    "supports_early_transfer_target_creation",
                    "_requires_upsert_partition_drop_template",
                    "_supports_distributed_table_targets",
                    "_uses_partition_replacement_upsert",
                ),
                modules=("analytics_toolkit.sql.core.capabilities.validate_write_mode",),
            ),
        )
    ]
    assert offenders == []


def test_generic_sql_backend_branch_debt_does_not_increase() -> None:
    allowed_counts = {
        # Configuration normalization intentionally owns these backend defaults.
        "analytics_toolkit/sql/connection/ddl_defaults.py": 5,
        "analytics_toolkit/sql/ddl/api.py": 0,
        "analytics_toolkit/sql/ddl/builders.py": 0,
        "analytics_toolkit/sql/ddl/extract_ddl.py": 3,
        "analytics_toolkit/sql/dml/io/execute_sql.py": 0,
        "analytics_toolkit/sql/dml/load/load_df.py": 0,
        "analytics_toolkit/sql/dml/load/load_sql_table.py": 0,
        "analytics_toolkit/sql/dml/table/create_table_from_sql.py": 0,
        "analytics_toolkit/sql/dml/table/drop_tables.py": 0,
        "analytics_toolkit/sql/dml/table/maintenance.py": 1,
        "analytics_toolkit/sql/dml/table/partitions.py": 1,
        "analytics_toolkit/sql/dml/table/write_modes.py": 0,
        "analytics_toolkit/sql/dml/transfer/flow/api.py": 0,
        "analytics_toolkit/sql/dml/transfer/flow/attempt.py": 0,
        "analytics_toolkit/sql/dml/transfer/flow/finalize.py": 0,
        "analytics_toolkit/sql/dml/transfer/flow/options.py": 0,
        "analytics_toolkit/sql/dml/transfer/flow/stage.py": 0,
        "analytics_toolkit/sql/dml/transfer/io/source.py": 0,
        "analytics_toolkit/sql/dml/transfer/staging.py": 0,
        "analytics_toolkit/sql/execution/operation_runner.py": 0,
        "analytics_toolkit/sql/execution/plan_steps.py": 0,
        "analytics_toolkit/sql/metadata/show_tables.py": 4,
        "analytics_toolkit/sql/metadata/table_info.py": 1,
    }
    excluded_parts = {"backends"}
    offenders: list[str] = []

    for path in SQL_ROOT.rglob("*.py"):
        relative = str(path.relative_to(PROJECT_ROOT))
        if any(part in excluded_parts for part in path.parts):
            continue
        count = backend_branch_count(ast.parse(path.read_text()))
        if count > allowed_counts.get(relative, 0):
            offenders.append(
                f"{relative}: {count} backend branches (allowed {allowed_counts.get(relative, 0)})"
            )

    assert offenders == []


def test_generic_sql_modules_do_not_pin_literal_backend_sets() -> None:
    offenders = []
    for path in SQL_ROOT.rglob("*.py"):
        if "backends" in path.parts:
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Set) and all(
                isinstance(item, ast.Constant) for item in node.elts
            ):
                values = {item.value for item in node.elts}
                if values == {"gp", "trino", "ch"}:
                    offenders.append(str(path.relative_to(PROJECT_ROOT)))
    assert offenders == []


def test_legacy_sql_compatibility_paths_are_removed() -> None:
    removed_files = {
        SQL_ROOT / "backend_adapters.py",
        SQL_ROOT / "ddl" / "clickhouse.py",
        SQL_ROOT / "dml" / "table" / "ch_create_table_as.py",
        SQL_ROOT / "dml" / "transfer" / "flow" / "estimate.py",
    }
    removed_package_files = {
        *(SQL_ROOT / "_backend_adapters").glob("*.py"),
        *(SQL_ROOT / "clickhouse").glob("*.py"),
    }
    assert [
        str(path.relative_to(PROJECT_ROOT))
        for path in removed_files | removed_package_files
        if path.exists()
    ] == []

    forbidden_modules = {
        "analytics_toolkit.sql.backend_adapters",
        "analytics_toolkit.sql._backend_adapters",
        "analytics_toolkit.sql.clickhouse.lifecycle",
        "analytics_toolkit.sql.clickhouse.options",
        "analytics_toolkit.sql.clickhouse.wait",
        "analytics_toolkit.sql.ddl.clickhouse",
        "analytics_toolkit.sql.dml.transfer.flow.estimate",
    }
    offenders = [
        f"{path.relative_to(PROJECT_ROOT)}: {module}"
        for path in SQL_ROOT.rglob("*.py")
        for module in imported_modules(path)
        if any(
            module == forbidden or module.startswith(forbidden + ".")
            for forbidden in forbidden_modules
        )
    ]
    assert offenders == []
    tree = ast.parse((SQL_ROOT / "dml" / "transfer" / "schema.py").read_text())
    assert "refine_ch_column_types_nullability_from_rows" not in referenced_names(tree)


def test_load_insert_backend_serialization_is_adapter_owned() -> None:
    load_path = SQL_ROOT / "dml" / "load" / "load_sql_table.py"
    offenders = [
        f"{path.relative_to(PROJECT_ROOT)}: {violation}"
        for path in [load_path]
        for violation in forbidden_code(
            path,
            CodeRules(
                modules=("psycopg2.extras", "decimal.Decimal"),
                names=("TrinoConfig", "SqlConfigError", "normalized_target_type", "to_pydatetime"),
                expressions=("isinstance(value, Decimal)",),
                calls_with=(("applymap", "_normalize_ch_scalar"),),
            ),
        )
    ]
    assert offenders == []


def test_load_sql_insert_dispatch_is_adapter_owned() -> None:
    path = SQL_ROOT / "dml" / "load" / "load_sql_table.py"
    tree = ast.parse(path.read_text())
    forbidden_names = {
        "DEFAULT_GP_INSERT_CHUNK_SIZE",
        "DEFAULT_TRINO_INSERT_CHUNK_SIZE",
        "_BATCH_INSERT_BACKENDS",
        "_ROW_INSERT_BACKENDS",
        "_make_batch_insert_backend",
        "_make_row_insert_backend",
        "_insert_trino_batch_backend",
        "_insert_gp_batch_backend",
        "_insert_ch_batch_backend",
        "_insert_trino_rows_backend",
        "_insert_gp_rows_backend",
        "_insert_ch_rows_backend",
    }
    assert not (referenced_names(tree) & forbidden_names)
    assert {"insert_dataframe_batch", "insert_rows_batch"} <= called_names(tree)


def test_parquet_load_stage_uses_target_backend_adapter() -> None:
    path = SQL_ROOT / "dml" / "load" / "load_df.py"
    tree = ast.parse(path.read_text())
    assert not contains_expression(tree, "get_backend_adapter('trino')")
    offenders = [
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"table_exists", "build_stage_table_name"}
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "trino"
    ]
    assert offenders == []


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

    assert [path.relative_to(PROJECT_ROOT) for path in removed_paths if path.exists()] == []


def test_show_tables_catalog_filter_policy_is_adapter_owned() -> None:
    path = SQL_ROOT / "metadata" / "show_tables.py"
    offenders = [
        f"{path.relative_to(PROJECT_ROOT)}: {violation}"
        for path in [path]
        for violation in forbidden_code(
            path, CodeRules(names=("get_backend_capability", "supports_show_tables_catalog_filter"))
        )
    ]
    assert offenders == []


def test_single_db_sql_public_operations_use_db_key() -> None:
    from analytics_toolkit import sql

    single_db_operations = {
        "create_table",
        "create_sql_table",
        "cancel_queries",
        "ch_reconfigure_table",
        "drop_partitions",
        "drop_tables",
        "execute",
        "execute_create",
        "execute_insert",
        "execute_read",
        "extract_ddl",
        "gp_create_partitions",
        "gp_vacuum",
        "load_df",
        "insert",
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


def test_sql_backend_registry_is_canonical_backend_list() -> None:
    from analytics_toolkit.sql.backends import BACKEND_REGISTRY, get_backend_names
    from analytics_toolkit.sql.connection.config import SUPPORTED_BACKENDS
    from analytics_toolkit.sql.core.capabilities import BACKEND_CAPABILITIES

    backend_names = set(get_backend_names())
    assert backend_names == set(BACKEND_REGISTRY)
    assert backend_names == SUPPORTED_BACKENDS
    assert set(BACKEND_CAPABILITIES) == backend_names
    assert {
        backend_name: backend.capability for backend_name, backend in BACKEND_REGISTRY.items()
    } == BACKEND_CAPABILITIES


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
