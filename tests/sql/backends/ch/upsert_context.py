"""A verified local partition upsert cannot grant a general destructive bypass."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.routing import local_sql
from analytics_toolkit.sql.backends.ch.upsert_context import (
    verified_local_partition_alter,
    verified_local_upsert,
)
from analytics_toolkit.sql.execution.backend_options import backend_operation
from sqlglot import exp, parse_one


def partition_alter(table="db.local"):
    return parse_one(f"ALTER TABLE {table} DROP PARTITION 1", read=ClickHouseDialect)


def changed(statement, key, value):
    result = statement.copy()
    result.set(key, value)
    return result


def test_verified_local_upsert_requires_exact_connection_target_and_partition_action():
    connection = object()
    statement = partition_alter('"a.b"."Local"')
    assert not verified_local_partition_alter(connection, statement)
    with verified_local_upsert(connection, '"a.b"."Local"'):
        assert verified_local_partition_alter(connection, statement)
        assert not verified_local_partition_alter(object(), statement)
        assert not verified_local_partition_alter(connection, partition_alter('a."b.Local"'))
        assert not verified_local_partition_alter(connection, partition_alter('"a.b"."local"'))
        assert not verified_local_partition_alter(connection, exp.Drop(this=statement.this))
        assert not verified_local_partition_alter(
            connection, changed(statement, "kind", "DATABASE")
        )
        assert not verified_local_partition_alter(connection, changed(statement, "actions", []))
        assert not verified_local_partition_alter(
            connection, changed(statement, "actions", [exp.Drop(this=exp.column("id"))])
        )
        assert not verified_local_partition_alter(
            connection,
            changed(statement, "cluster", exp.OnCluster(this=exp.Literal.string("core"))),
        )
        assert not verified_local_partition_alter(
            connection, changed(statement, "this", exp.Table(this=exp.Anonymous(this="numbers")))
        )


def test_verified_local_upsert_resets_nested_context_after_exception_and_isolates_threads():
    connection = object()
    statement = partition_alter()
    with verified_local_upsert(connection, "db.local"):

        def fail_inner():
            with verified_local_upsert(connection, "db.other"):
                assert not verified_local_partition_alter(connection, statement)
                message = "inner"
                raise RuntimeError(message)

        with pytest.raises(RuntimeError, match="inner"):
            fail_inner()
        assert verified_local_partition_alter(connection, statement)
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert not pool.submit(verified_local_partition_alter, connection, statement).result()
    assert not verified_local_partition_alter(connection, statement)
    invalid = verified_local_upsert(connection, "numbers(1)")
    with pytest.raises(ValueError, match="static physical"), invalid:
        pytest.fail("dynamic target accepted")


@pytest.mark.parametrize("binding", ["verified", "ordinary", "wrong_target", "wrong_connection"])
def test_verified_local_upsert_outage_fallback_requires_exact_scoped_binding(outage, binding):
    cluster, _ = outage
    cluster.alive.remove("h2")
    connection = sql.get_sql_connection("ch")
    table = "db.other" if binding == "wrong_target" else "db.local"
    bound_connection = object() if binding == "wrong_connection" else connection
    context = (
        nullcontext() if binding == "ordinary" else verified_local_upsert(bound_connection, table)
    )
    with context, local_sql(connection):
        if binding == "verified":
            connection.command("ALTER TABLE db.local DROP PARTITION 1")
            assert len(cluster.commands) == 1
            assert "ON CLUSTER" not in cluster.commands[0][1]
        else:
            with pytest.raises(sql.ClickHouseClusterUnavailableError):
                connection.command("ALTER TABLE db.local DROP PARTITION 1")
            assert not cluster.commands


@pytest.mark.parametrize("query", ["ALTER TABLE db.local DROP COLUMN id", "DROP TABLE db.local"])
def test_verified_local_upsert_other_destructive_actions_keep_outage_preflight(outage, query):
    cluster, _ = outage
    cluster.alive.remove("h2")
    connection = sql.get_sql_connection("ch")
    failure = pytest.raises(sql.ClickHouseClusterUnavailableError)
    with verified_local_upsert(connection, "db.local"), local_sql(connection), failure:
        connection.command(query)
    assert not cluster.commands


@pytest.mark.parametrize("recovery", [False, True])
def test_verified_local_upsert_keeps_partial_and_recovery_guards(outage, recovery):
    cluster, _ = outage
    connection = sql.get_sql_connection("ch")
    with backend_operation() as state:
        state.preserve_recovery = recovery
        state.observed_partial = not recovery
        failure = pytest.raises(sql.ClickHouseClusterUnavailableError, match=r"Recovery|complete")
        with verified_local_upsert(connection, "db.local"), local_sql(connection), failure:
            connection.command("ALTER TABLE db.local DROP PARTITION 1")
    assert not cluster.commands


def test_verified_local_upsert_without_local_execution_keeps_cluster_preflight(outage):
    cluster, _ = outage
    cluster.alive.remove("h2")
    connection = sql.get_sql_connection("ch")
    failure = pytest.raises(sql.ClickHouseClusterUnavailableError)
    with verified_local_upsert(connection, "db.local"), failure:
        connection.command("ALTER TABLE db.local DROP PARTITION 1")
    assert not cluster.commands
