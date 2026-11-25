"""The original keyed shared/worker creation paths register acknowledgements."""

from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.connection.errors import SqlTableReadinessError
from analytics_toolkit.sql.ddl.ownership import execute_owned_create_sqls
from analytics_toolkit.sql.dml.transfer.runtime.models import (
    TransferConnectionRefs,
    TransferStageState,
)
from analytics_toolkit.sql.execution.backend_options import backend_operation
from sqlglot import exp, parse_one

from tests.sql._support.ch_lifecycle import install_lifecycle
from tests.sql._support.reconfigure_conversion_scope import _scope_sql
from tests.sql._support.row_batches import (
    attempt_module,
    finalize_module,
    keys_module,
    make_progress_options,
)


@pytest.mark.parametrize("parallel_workers", [False, True])
def test_keyed_initialization_registers_partial_pair_before_readiness_returns(
    monkeypatch: pytest.MonkeyPatch, write_sql_connections: Any, parallel_workers: bool
) -> None:
    server = install_lifecycle(monkeypatch, write_sql_connections, driver="http")
    original = server.snapshot()
    slices = [
        keys_module.build_transfer_slice(
            index=index,
            source_sql="SELECT id FROM source WHERE {id}",
            transfer_keys=[keys_module.TransferKey("id", "id")],
            values=(index,),
        )
        for index in range(2)
    ]
    options = make_progress_options(
        to_db_key="ch",
        to_db_backend="ch",
        target_table="analytics.target",
        write_mode="append",
        table_schema={"id": "Int64"},
        transfer_slices=slices,
    )
    options = replace(
        options,
        transfer_concurrency=replace(
            options.transfer_concurrency,
            effective_write=2 if parallel_workers else 1,
        ),
    )
    state = TransferStageState(target_exists=True)

    def create_stage(**kwargs: Any) -> str:
        kwargs["on_stage_candidate"]("analytics.partial_stage")
        execute_owned_create_sqls(
            get_backend_adapter("ch"),
            kwargs["connection"],
            [
                "CREATE TABLE analytics.partial_stage_shard ON CLUSTER storage "
                "(id Int64) ENGINE=Memory"
            ],
            kwargs["on_resource_created"],
            on_scoped_resource_created=kwargs["on_scoped_resource_created"],
            connection_key="ch",
        )
        message = "shard created before readiness failed"
        raise SqlTableReadinessError(message)

    monkeypatch.setattr(attempt_module, "create_stage_table", create_stage)
    with backend_operation(), closing(sql.get_sql_connection("ch")) as connection:
        with pytest.raises(SqlTableReadinessError):
            attempt_module.initialize_shared_stage_for_keyed_slices(
                options=options,
                connection_refs=TransferConnectionRefs(target={"connection": connection}),
                stage_state=state,
                source_schema=[SimpleNamespace(name="id")],
            )
        assert state.stage_table is None
        assert not state.stage_table_created
        assert state.stage_table_candidates == ["analytics.partial_stage"]
        assert state.owned_stage_resources == [
            ("analytics.partial_stage_shard", "analytics.partial_stage")
        ]
        assert len(state.owned_stage_scoped_resources) == 1
        resource, stage = state.owned_stage_scoped_resources[0]
        assert resource.certificate is not None
        assert stage == "analytics.partial_stage"
        monkeypatch.setattr(
            finalize_module,
            "_run_target_operation",
            lambda _options, _role, operation, **_kwargs: operation({"connection": connection}),
        )
        finalize_module.cleanup_stage(options, object(), state, 1)
    dropped = []
    for statement in server.commands:
        cluster, body = _scope_sql(statement)
        tree = parse_one(body, read=ClickHouseDialect)
        if isinstance(tree, exp.Drop):
            table = tree.find(exp.Table)
            dropped.append((cluster, table.db, table.name))
    assert dropped == [("storage", "analytics", "partial_stage_shard")]
    assert server.snapshot() == original
    assert all(connection.close_calls == 1 for connection in server.opened)
