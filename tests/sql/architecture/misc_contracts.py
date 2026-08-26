from __future__ import annotations

from tests.sql._support.cross_area import (
    inspect,
    pytest,
    sql_module,
)


@pytest.mark.parametrize(
    "function_name",
    [
        "drop_partitions",
        "drop_tables",
        "create_sql_table",
        "ch_reconfigure_table",
        "execute",
        "gp_create_partitions",
        "load_df",
        "transfer",
    ],
)
def test_public_mutating_sql_helpers_accept_dry_run_plan_options(
    function_name: str,
) -> None:
    signature = inspect.signature(getattr(sql_module, function_name))

    assert "dry_run" in signature.parameters
    assert "return_sql" in signature.parameters
