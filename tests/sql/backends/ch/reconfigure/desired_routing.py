"""Desired routing is certified at every actual facade execution endpoint."""

from __future__ import annotations

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.execution.plans import SqlPlan

from tests.sql._support.ch_facade_routes import install_facade_routes


def _rebuild(cluster, *, dry_run):
    return sql.ch_reconfigure_table(
        "ch",
        "analytics.events",
        order_by="id DESC",
        ch_distributed_cluster=cluster,
        ch_distributed_on_cluster="core",
        dry_run=dry_run,
        return_metadata=True,
        retry_cnt=1,
        timeout_increment=0,
    )


@pytest.mark.parametrize("driver", ["http", "native"])
@pytest.mark.parametrize("cluster", ["desired", "{route}"])
def test_public_rebuild_rejects_desired_route_that_diverges_on_a_remote_facade(
    monkeypatch, write_sql_connections, driver, cluster
):
    state = install_facade_routes(monkeypatch, write_sql_connections, driver=driver)
    name = "remote_alias" if cluster.startswith("{") else cluster
    state.route_members[state.remote, name] = state.groups["split"]
    before = state.snapshot()

    with pytest.raises(InvalidSqlInputError, match=r"[Rr]outing|[Ss]hard|[Gg]roup"):
        _rebuild(cluster, dry_run=True)

    assert state.snapshot() == before
    assert state.commands == []
    assert all(connection.close_calls == 1 for connection in state.opened)


def test_public_rebuild_cannot_publish_a_remote_facade_that_duplicates_replicated_rows(
    monkeypatch, write_sql_connections
):
    state = install_facade_routes(monkeypatch, write_sql_connections, driver="http")
    state.route_members[state.remote, "desired"] = state.groups["split"]
    before = state.snapshot()
    error = None
    result = None

    try:
        result = _rebuild("desired", dry_run=False)
    except InvalidSqlInputError as caught:
        error = caught

    diagnostic = {
        "result": repr(result),
        "remote_rows": state.rows(state.remote, "analytics.events"),
        "commands": list(state.commands),
    }
    assert state.snapshot() == before, repr(diagnostic)
    assert isinstance(error, InvalidSqlInputError), repr(error)
    assert state.commands == []
    assert all(connection.close_calls == 1 for connection in state.opened)


@pytest.mark.parametrize("driver", ["http", "native"])
@pytest.mark.parametrize("cluster", ["desired", "{route}"])
def test_public_rebuild_accepts_equivalent_per_host_desired_aliases_and_macros(
    monkeypatch, write_sql_connections, driver, cluster
):
    state = install_facade_routes(monkeypatch, write_sql_connections, driver=driver)
    before = state.snapshot()

    plan = _rebuild(cluster, dry_run=True)

    assert isinstance(plan, SqlPlan)
    assert plan.options["strategy"] == "managed_pair_rebuild"
    assert state.snapshot() == before
    assert state.commands == []
    assert all(connection.close_calls == 1 for connection in state.opened)
