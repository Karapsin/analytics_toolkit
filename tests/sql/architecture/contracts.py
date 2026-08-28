from __future__ import annotations

from tests.sql._support.cross_area import (
    backend_registry_module,
    capabilities_module,
    inspect,
    pd,
    plans_module,
    pytest,
    sql_module,
)


def test_backend_support_matrix_includes_write_modes() -> None:
    rows = capabilities_module.support_matrix_rows()

    assert {row["backend"] for row in rows} == set(backend_registry_module.get_backend_names())
    for row in rows:
        assert "truncate_insert" in row["write_modes"]
        assert "upsert" in row["write_modes"]
        assert (
            capabilities_module.validate_write_mode(
                row["backend"],
                "upsert",
            )
            == "upsert"
        )


def test_capability_directory_lists_lazy_exports() -> None:
    assert "BackendCapability" in capabilities_module.__dir__()
    assert "WriteMode" in dir(capabilities_module)
    assert capabilities_module.BackendCapability is backend_registry_module.BackendCapability


def test_public_sql_type_aliases_are_exported() -> None:
    assert sql_module.BackendName is not None
    assert sql_module.ConnectionKey is str
    assert sql_module.SqlText is str
    assert sql_module.TableName is str
    assert sql_module.SqlTaskType is not None


def test_public_sql_facade_exports_refactored_helpers() -> None:
    assert sql_module.async_sql is not None
    assert sql_module.parallel_sql is not None
    assert sql_module.show_tables is not None
    assert sql_module.table_info is not None
    assert sql_module.format_plan is plans_module.format_plan
    assert sql_module.BACKEND_CAPABILITIES is capabilities_module.BACKEND_CAPABILITIES


def test_public_sql_facade_exports_safety_contracts() -> None:
    assert sql_module.AmbiguousSqlMutationError is not None
    assert sql_module.AmbiguousSqlReplaceError is not None
    assert sql_module.ClickHouseClusterTopologyError is not None
    assert sql_module.EmptySourceError is not None
    assert sql_module.EmptySourcePolicy is not None
    assert sql_module.ExecuteRetryPolicy is not None
    assert sql_module.SqlBatchExecutionError is not None
    assert sql_module.SqlBatchItemResult is not None
    assert sql_module.SqlBatchItemStatus is not None


@pytest.mark.parametrize(
    "function_name",
    [
        "async_sql",
        "execute_read",
        "execute",
        "load_df",
        "parallel_sql",
        "transfer",
    ],
)
def test_public_sql_progress_defaults_to_false(function_name: str) -> None:
    signature = inspect.signature(getattr(sql_module, function_name))

    assert signature.parameters["progress"].default is False


def test_public_sql_function_logs_total_elapsed_for_dry_run(capsys) -> None:
    plan = sql_module.load_df(
        "gp",
        "sandbox.scores",
        pd.DataFrame({"user_id": [1], "score": [10]}),
        write_mode="truncate_insert",
        dry_run=True,
    )

    output = capsys.readouterr().out
    assert plan.operation == "load_df"
    assert "[load_df] [timing] Finished SQL function in " in output
