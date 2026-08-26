from __future__ import annotations

from tests.sql._support.adapters import (
    BACKEND_REGISTRY,
    BackendAdapter,
    SqlConfigError,
    UnsupportedConnectionTypeError,
    backend_capability_map,
    backend_registry_module,
    get_backend,
    get_backend_capability,
    importlib,
    normalize_backend_name,
    pytest,
    require_backend_name,
    supported_backend_message,
)


def test_backend_lookup_preserves_connection_config_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    connections_path = tmp_path / ".connections"
    connections_path.unlink()
    with pytest.raises(SqlConfigError, match="Missing SQL connections file"):
        get_backend("missing_alias")

    connections_path.write_text("{", encoding="utf-8")
    with pytest.raises(SqlConfigError, match="must contain valid JSON"):
        get_backend("missing_alias")


def test_backend_lookup_preserves_unknown_connection_key_errors() -> None:
    with pytest.raises(UnsupportedConnectionTypeError, match="Unknown SQL connection key"):
        get_backend("missing_alias")


def test_backend_registry_normalizes_aliases_and_reports_supported_names() -> None:
    assert normalize_backend_name(" PostgreSQL ") == "gp"
    assert normalize_backend_name("clickhouse-connect") == "ch"
    assert require_backend_name(" TRINO ", connection_key="warehouse") == "trino"
    assert supported_backend_message() == "Expected one of: ch, gp, trino."
    assert set(backend_capability_map()) == {"ch", "gp", "trino"}

    with pytest.raises(UnsupportedConnectionTypeError, match="backend 'Oracle'"):
        normalize_backend_name("Oracle")
    with pytest.raises(
        UnsupportedConnectionTypeError,
        match=r"connection 'warehouse'.*unsupported type 'postgres'",
    ):
        require_backend_name("postgres", connection_key="warehouse")


def test_backend_registry_rejects_invalid_backend_returned_by_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_module = importlib.import_module("analytics_toolkit.sql.connection.config")
    monkeypatch.setattr(
        config_module,
        "get_connection_backend",
        lambda connection_key: "oracle",
    )

    with pytest.raises(
        UnsupportedConnectionTypeError,
        match="Unsupported connection type",
    ):
        backend_registry_module.get_backend("warehouse")


def test_registered_backends_implement_full_contract() -> None:
    required_methods = {
        "build_connection_config",
        "build_create_table_sqls",
        "copy_airflow_fields",
        "open_connection",
        "execute_command",
        "table_exists",
        "clear_table_sqls",
        "get_table_column_types",
        "inspect_source_query_schema",
        "map_source_type_to_target",
        "build_upsert_stage_sqls",
        "build_upsert_stage_placeholder_sqls",
        "execute_sql",
        "execute_read_sql",
        "insert_dataframe_batch",
        "insert_rows_batch",
        "running_query_ids_sql",
        "cancel_query_sql",
        "infer_dataframe_column_type",
    }
    inherited_contract_methods = {
        "build_insert_from_stage_sql",
        "build_insert_from_stage_placeholder_sql",
        "allows_show_tables_catalog_filter",
        "can_create_transfer_target_before_batches",
        "create_table_from_sql_fast_path",
        "build_create_from_sql_target_create_kwargs",
        "build_load_target_create_kwargs",
        "column_types_for_columns",
        "after_create_table",
        "expected_create_table_column_types",
        "requires_load_target_column_metadata",
        "refine_stage_column_types_from_rows",
        "needs_upsert_partition_drop_template",
        "normalize_ch_columns_or_expression",
        "normalize_ch_string",
        "resolve_ch_retry_per_host_drops",
        "resolve_transfer_stage_column_types",
        "resolve_transfer_staging_mode",
        "resolve_table_info_table_name",
        "should_analyze_table",
        "should_ensure_load_target_table",
        "should_insert_create_table_from_sql_directly",
        "supports_distributed_table_targets",
        "target_connection_defaults",
        "transfer_attempt_policy",
        "transfer_insert_page_sizing",
        "uses_partition_replacement_upsert",
        "validate_ch_create_table_options",
        "validate_ch_columns_in_columns",
        "validate_gp_distributed_by_key_option",
        "validate_gp_insert_chunk_size_option",
        "validate_trino_insert_chunk_size_option",
        "validate_write_mode",
    }
    missing: list[str] = []
    for backend_name, backend in BACKEND_REGISTRY.items():
        capability = get_backend_capability(backend_name)
        assert capability.name == backend_name
        assert capability == backend.capability
        assert backend.backend == backend_name
        for method_name in sorted(inherited_contract_methods):
            assert callable(getattr(backend, method_name))
        for method_name in sorted(required_methods):
            method = getattr(type(backend), method_name, None)
            if method is getattr(BackendAdapter, method_name, None):
                missing.append(f"{backend_name}.{method_name}")

    assert missing == []
