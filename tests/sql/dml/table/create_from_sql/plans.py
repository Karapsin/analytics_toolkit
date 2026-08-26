from __future__ import annotations

from tests.sql._support.create_from_sql import (
    create_module,
    pytest,
)


def test_create_table_from_sql_clickhouse_dry_fast_path_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = create_module.get_backend_adapter("ch")
    monkeypatch.setattr(
        adapter,
        "create_table_from_sql_fast_path",
        lambda **kwargs: (True, "fast plan"),
    )
    assert (
        create_module.create_table_from_sql(
            "ch",
            "events",
            "SELECT 1 AS id",
            dry_run=True,
        )
        == "fast plan"
    )


def test_create_table_from_sql_dry_run_uses_table_schema() -> None:
    plan = create_module.create_table_from_sql(
        "gp",
        "sandbox.target_table",
        "select id, amount from source_table",
        insert_data=False,
        dry_run=True,
        table_schema={"id": "TEXT", "amount": "NUMERIC(10, 2)"},
    )

    create_sql = next(
        statement.sql for statement in plan.statements if statement.phase == "create_target"
    )
    assert plan.options["table_schema"] == {
        "id": "TEXT",
        "amount": "NUMERIC(10, 2)",
    }
    assert '"id" TEXT' in create_sql
    assert '"amount" NUMERIC(10, 2)' in create_sql
