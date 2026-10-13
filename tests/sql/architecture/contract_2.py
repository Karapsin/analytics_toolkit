from __future__ import annotations

from tests._support.source import CodeRules, called_names, forbidden_code, imported_modules
from tests.sql._support.architecture import (
    PROJECT_ROOT,
    SQL_ROOT,
    ast,
    importlib,
    inspect,
    pkgutil,
    pytest,
)


def test_sql_facade_is_the_supported_public_surface() -> None:
    from analytics_toolkit import sql

    public_names = {
        "cancel_queries",
        "ch_reconfigure_table",
        "create_table",
        "create_sql_table",
        "drop_partitions",
        "drop_tables",
        "execute",
        "execute_create",
        "execute_insert",
        "generate_dummy_connections",
        "gp_create_partitions",
        "load_df",
        "insert",
        "read",
        "set_missing_secrets",
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


def test_sql_facade_reload_tolerates_stale_transfer_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sql_module = importlib.import_module("analytics_toolkit.sql")
    api_module = importlib.import_module("analytics_toolkit.sql.dml.transfer.flow.api")
    options_module = importlib.import_module("analytics_toolkit.sql.dml.transfer.flow.options")

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
            oversized.append(f"{path.relative_to(PROJECT_ROOT)} has {line_count} lines")

    assert oversized == []


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


def test_sql_source_does_not_restore_removed_aggregation_imports() -> None:
    forbidden_modules = {
        "analytics_toolkit.sql.ddl.create_sql_table",
        "analytics_toolkit.sql.dml.table.table_ops",
        "analytics_toolkit.sql.orchestration.async_api",
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


def test_stage_table_identifier_backend_policy_is_adapter_owned() -> None:
    offenders = [
        f"{path.relative_to(PROJECT_ROOT)}: {violation}"
        for path in [SQL_ROOT / "dml" / "load" / "stage.py"]
        for violation in forbidden_code(
            path,
            CodeRules(
                names=("GP_IDENTIFIER_MAX_BYTES",),
                expressions=("connection_type != 'gp'", "hashlib.sha1"),
            ),
        )
    ]
    assert offenders == []


def test_table_info_name_resolution_is_adapter_owned() -> None:
    table_info_path = SQL_ROOT / "metadata" / "table_info.py"
    offenders = [
        f"{path.relative_to(PROJECT_ROOT)}: {violation}"
        for path in [table_info_path]
        for violation in forbidden_code(
            path,
            CodeRules(
                names=("split_trino_table_name",),
                expressions=("backend != 'trino'", "backend == 'trino'"),
            ),
        )
    ]
    assert offenders == []
    assert "resolve_table_info_table_name" in called_names(ast.parse(table_info_path.read_text()))


def test_transfer_api_reload_tolerates_stale_transfer_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api_module = importlib.import_module("analytics_toolkit.sql.dml.transfer.flow.api")
    options_module = importlib.import_module("analytics_toolkit.sql.dml.transfer.flow.options")

    assert hasattr(options_module, "resolve_trino_mode")
    with monkeypatch.context() as patch:
        patch.delattr(options_module, "resolve_trino_mode")
        reloaded_api = importlib.reload(api_module)

    assert callable(reloaded_api.transfer_table)
    assert hasattr(options_module, "resolve_trino_mode")
    importlib.reload(api_module)


def test_transfer_insert_page_sizing_policy_is_adapter_owned() -> None:
    checked_paths = [
        SQL_ROOT / "dml" / "transfer" / "runtime" / "models.py",
        SQL_ROOT / "dml" / "transfer" / "flow" / "api.py",
        SQL_ROOT / "dml" / "transfer" / "flow" / "attempt.py",
    ]
    offenders = [
        f"{path.relative_to(PROJECT_ROOT)}: {violation}"
        for path in checked_paths
        for violation in forbidden_code(
            path,
            CodeRules(
                names=(
                    "DEFAULT_GP_INSERT_CHUNK_SIZE",
                    "make_gp_insert_chunk_sizer",
                    "supports_adaptive_transfer_insert_page_size",
                )
            ),
        )
    ]
    assert offenders == []


def test_trino_parquet_stage_sql_is_adapter_owned() -> None:
    generic_path = SQL_ROOT / "dml" / "transfer" / "flow" / "parquet_stage.py"
    offenders = [
        f"{path.relative_to(PROJECT_ROOT)}: {violation}"
        for path in [generic_path]
        for violation in forbidden_code(
            path,
            CodeRules(
                names=(
                    "parse_one",
                    "sqlglot",
                    "Decimal",
                    "datetime",
                    "_infer_trino_type_from_values",
                ),
                strings=("format = 'PARQUET'", "external_location = '"),
            ),
        )
    ]
    assert offenders == []


def test_upsert_backend_policy_is_capability_owned() -> None:
    assert not (SQL_ROOT / "dml" / "table" / "upsert_policy.py").exists()

    offenders = [
        f"{path.relative_to(PROJECT_ROOT)}: {violation}"
        for path in SQL_ROOT.rglob("*.py")
        if "backends" not in path.parts
        for violation in forbidden_code(
            path,
            CodeRules(
                names=(
                    "is_clickhouse_backend",
                    "is_trino_backend",
                    "PARTITION_REPLACEMENT_UPSERT_BACKENDS",
                )
            ),
        )
    ]
    assert offenders == []
