from __future__ import annotations

from types import SimpleNamespace

import pytest
from analytics_toolkit.sql.backends.ch.policy_drop import execute_policy_target_drop
from analytics_toolkit.sql.backends.ch.recovery_endpoints import RecoveryEndpoint
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError

from tests.sql._support.reconfigure import importlib

module = importlib.import_module("analytics_toolkit.sql.backends.ch.policy_drop")


def test_drop_keeps_exact_resource_identity_and_independent_execution_scopes(monkeypatch):
    emitted = []
    waited = []
    adapter = SimpleNamespace(execute_commands=lambda _connection, sqls: emitted.extend(sqls))
    monkeypatch.setattr(
        module, "_wait_for_ch_table_absence", lambda _c, table: waited.append((table, None))
    )
    monkeypatch.setattr(
        module,
        "_wait_for_ch_table_absence_on_cluster",
        lambda _c, table, *, ch_cluster: waited.append((table, ch_cluster)),
    )
    sqls = [
        "DROP TABLE IF EXISTS `analytics.db`.`events`",
        "DROP TABLE IF EXISTS `analytics.db`.`events` ON CLUSTER facades",
        "DROP TABLE IF EXISTS `analytics.db`.`physical` ON CLUSTER storage",
    ]
    execute_policy_target_drop(adapter, object(), sqls, target_key="ch")
    assert emitted == sqls
    assert waited == [
        ('"analytics.db"."events"', None),
        ('"analytics.db"."events"', "facades"),
        ('"analytics.db"."physical"', "storage"),
    ]


@pytest.mark.parametrize("scope", [None, "core"])
def test_absence_timeout_preserves_failure_when_host_retry_is_disabled_or_unavailable(
    monkeypatch, scope
):
    error = TimeoutError("still visible")

    def wait(*_args, **_kwargs):
        raise error

    adapter = SimpleNamespace(execute_commands=lambda *_args: None)
    monkeypatch.setattr(module, "_wait_for_ch_table_absence", wait)
    monkeypatch.setattr(module, "_wait_for_ch_table_absence_on_cluster", wait)
    monkeypatch.setattr(
        module,
        "_drop_ch_distributed_table_pair_on_cluster_hosts",
        lambda *_a, **_k: pytest.fail("unexpected host replay"),
    )
    suffix = " ON CLUSTER core" if scope is not None else ""
    with pytest.raises(TimeoutError) as caught:
        execute_policy_target_drop(
            adapter,
            object(),
            [f"DROP TABLE IF EXISTS db.events{suffix}"],
            target_key="ch",
            retry_per_host_drops=scope is None,
        )
    assert caught.value is error


def test_host_recovery_never_invents_companion_names_and_keeps_native_port(monkeypatch):
    attempts = []
    recovered = []
    opened = []
    endpoint = RecoveryEndpoint("host", 9011)
    adapter = SimpleNamespace(execute_commands=lambda *_args: None)

    def wait(_connection, table, *, ch_cluster):
        attempts.append((table, ch_cluster))
        if len(attempts) == 1:
            error = TimeoutError("drop pending")
            raise error

    def recover(_connection, pair, **kwargs):
        recovered.append((pair.distributed_table, pair.shard_table, kwargs["ch_cluster"]))
        kwargs["per_host_connection_factory"](endpoint)

    monkeypatch.setattr(module, "_wait_for_ch_table_absence_on_cluster", wait)
    monkeypatch.setattr(module, "_drop_ch_distributed_table_pair_on_cluster_hosts", recover)
    monkeypatch.setattr(
        module, "get_ch_connection_for_host", lambda key, host: opened.append((key, host))
    )
    execute_policy_target_drop(
        adapter, object(), ["DROP TABLE IF EXISTS db.physical ON CLUSTER core"], target_key="alias"
    )
    assert attempts == [("db.physical", "core"), ("db.physical", "core")]
    assert recovered == [("db.physical", "db.physical", "core")]
    assert opened[0][0] == "alias"
    assert opened[0][1].tcp_port == 9011


@pytest.mark.parametrize(
    "sql", ["SELECT 1", "DROP TABLE db.a, db.b", "DROP TABLE db.a ON CLUSTER {name:String}"]
)
def test_unverifiable_drop_batches_fail_before_any_submission(sql):
    emitted = []
    adapter = SimpleNamespace(execute_commands=lambda _c, sqls: emitted.extend(sqls))
    with pytest.raises(InvalidSqlInputError):
        execute_policy_target_drop(adapter, object(), [sql], target_key="ch")
    assert emitted == []


def test_empty_drop_batch_has_no_backend_side_effects():
    execute_policy_target_drop(object(), object(), [], target_key="ch")
