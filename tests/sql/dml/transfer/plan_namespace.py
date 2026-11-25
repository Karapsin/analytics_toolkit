"""Dry SQL stages use the same configured namespace as runtime stage creation."""

from __future__ import annotations

from copy import deepcopy

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.core.identifiers import TableIdentifier
from analytics_toolkit.sql.dml.load.stage import build_stage_table_name

from tests.conftest import DEFAULT_SQL_CONNECTIONS


@pytest.mark.parametrize(
    ("backend", "alias"), [("gp", "gp_sandbox"), ("ch", "ch"), ("trino", "trino")]
)
def test_public_transfer_sql_stage_plan_uses_runtime_sql_namespace(
    monkeypatch, write_sql_connections, backend, alias
):
    configs = deepcopy(DEFAULT_SQL_CONNECTIONS)
    configs["gp"]["transfer_staging_schema"] = None
    configs[alias]["transfer_staging_schema"] = "scratch"
    if backend == "trino":
        configs[alias]["s3_transfer_staging_schema"] = "separate_hive_stage"
        configs[alias]["s3_transfer_staging_location"] = "s3://owned-test/stage"
    write_sql_connections(configs)

    def unexpected_connection(*_args, **_kwargs):
        pytest.fail("A dry transfer must not open a database connection")

    for selected in {"gp", backend}:
        monkeypatch.setattr(get_backend_adapter(selected), "open_connection", unexpected_connection)

    plan = sql.transfer(
        "gp",
        alias,
        from_sql="SELECT id FROM sandbox.source",
        to_table="analytics.events",
        table_schema={"id": "Int64" if backend == "ch" else "BIGINT"},
        dry_run=True,
        ignore_source_staging=True,
        **({"trino_mode": "values"} if backend == "trino" else {}),
    )

    stage_names = plan.options["stage_tables"]
    assert len(stage_names) == 1
    planned = TableIdentifier.parse(stage_names[0], backend)
    runtime = TableIdentifier.parse(
        build_stage_table_name(
            backend,
            "analytics.events",
            transfer_staging_schema="scratch",
            random_suffix="a" * 32 + "__w00000",
        ),
        backend,
    )
    assert planned.parts[:-1] == runtime.parts[:-1] == ("scratch",)
    assert any(statement.target_table in stage_names for statement in plan.statements)
    assert all(
        TableIdentifier.parse(statement.target_table, backend).parts[:-1] == ("scratch",)
        for statement in plan.statements
        if statement.target_table in stage_names
    )
