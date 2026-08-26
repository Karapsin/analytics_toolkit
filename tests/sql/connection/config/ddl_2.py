from __future__ import annotations

from tests.sql._support.connection_config import (
    create_sql_table_module,
    pd,
    pytest,
)


def test_create_table_with_connection_returns_plan_and_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        create_sql_table_module,
        "_build_create_sql_table_sqls",
        lambda options, option_owner: ["create table target (id bigint)"],
    )
    monkeypatch.setattr(
        create_sql_table_module,
        "_execute_create_sql_table",
        lambda **kwargs: None,
    )

    plan = create_sql_table_module._create_sql_table_with_connection(
        "gp",
        object(),
        "target",
        pd.DataFrame({"id": [1]}),
        dry_run=True,
    )
    result = create_sql_table_module._create_sql_table_with_connection(
        "gp",
        object(),
        "target",
        pd.DataFrame({"id": [1]}),
        return_metadata=True,
    )

    assert plan.operation == "create_table"
    assert result.metadata.statement_count == 1
