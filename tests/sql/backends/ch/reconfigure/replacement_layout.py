from __future__ import annotations

from types import SimpleNamespace

import pytest
from analytics_toolkit.sql.backends.ch.creation_policy import ClickHouseCreationPolicy
from analytics_toolkit.sql.backends.ch.replace_layout import prepare_replacement_layout
from analytics_toolkit.sql.connection.errors import AmbiguousSqlReplaceError

from tests.sql._support.reconfigure import (
    LocalReconfigureClient,
    ReconfigureClient,
    get_backend_adapter,
    importlib,
    reconfigure_backend,
)

module = importlib.import_module("analytics_toolkit.sql.backends.ch.replace_layout")


def target_policy(*, pair: bool) -> ClickHouseCreationPolicy:
    return ClickHouseCreationPolicy(
        create_distributed_pair=pair,
        shard_engine="ReplicatedMergeTree",
        shard_on_cluster="core" if pair else None,
        distributed_engine_template=(
            "Distributed({cluster}, {database}, {shard_table}, {sharding_key}, 'extra')"
        ),
        distributed_cluster="core" if pair else None,
        distributed_on_cluster="core" if pair else None,
        sharding_key="cityHash64(new_only_column)",
        ddl_ready_timeout_seconds=1,
    )


@pytest.mark.parametrize("pair", [False, True])
def test_guarded_replacement_converts_verified_existing_topology_before_applying_new_schema(
    monkeypatch, pair
):
    class QuotedClient(ReconfigureClient):
        def query(self, text, settings=None):
            return super().query(text.replace("`", ""), settings=settings)

    client = LocalReconfigureClient() if pair else QuotedClient()
    target = "analytics.local_events" if pair else "analytics.events"
    request = SimpleNamespace(
        connection=client,
        target_table=target,
        connection_key="ch",
        query_label=None,
        ch_only_shard=not pair,
        ch_creation_policy=target_policy(pair=pair),
    )
    monkeypatch.setattr(reconfigure_backend, "_wait_for_created_replacement", lambda *_: None)
    monkeypatch.setattr(reconfigure_backend, "_wait_for_cleanup", lambda *_: None)
    prepare_replacement_layout(get_backend_adapter("ch"), request)
    assert any(sql.startswith("INSERT INTO") for sql in client.commands)
    assert any(sql.startswith("RENAME TABLE") for sql in client.commands)
    # A new-only sharding expression cannot be evaluated while preserving the
    # original data. The final guarded replacement installs the requested key.
    assert all("new_only_column" not in sql for sql in client.commands)
    if pair:
        facade = next(sql for sql in client.commands if "ENGINE=Distributed" in sql)
        assert "'extra'" in facade
        assert "rand()" in facade
    else:
        insert = next(sql for sql in client.commands if sql.startswith("INSERT INTO"))
        assert "__physical_" in insert
        assert "cluster(" not in insert


def test_guarded_replacement_retains_incomplete_conversion_recovery(monkeypatch):
    client = LocalReconfigureClient()
    request = SimpleNamespace(
        connection=client,
        target_table="analytics.local_events",
        connection_key="ch",
        query_label=None,
        ch_only_shard=False,
        ch_creation_policy=target_policy(pair=True),
    )
    monkeypatch.setattr(module, "execute_ch_table_reconfiguration", lambda *_a, **_k: None)
    with pytest.raises(AmbiguousSqlReplaceError, match="retained recovery artifacts"):
        prepare_replacement_layout(get_backend_adapter("ch"), request)
    assert client.commands == []


def test_completed_conversion_without_physical_certificate_stops_replacement(monkeypatch):
    class QuotedClient(ReconfigureClient):
        def query(self, text, settings=None):
            return super().query(text.replace("`", ""), settings=settings)

    client = QuotedClient()
    request = SimpleNamespace(
        connection=client,
        target_table="analytics.events",
        connection_key="ch",
        query_label=None,
        ch_only_shard=True,
        ch_creation_policy=target_policy(pair=False),
    )
    original_inspect = module.inspect_existing_layout
    inspections = []
    completed_changes = []

    def inspect_initial_layout(*args, **kwargs):
        assert inspections == [], "A completed conversion used an unverified fresh lookup."
        inspections.append(args[1])
        return original_inspect(*args, **kwargs)

    def acknowledge_without_destination_certificate(_adapter, _connection, change, **_kwargs):
        assert change.physical_execution is not None
        change.cleanup_complete = True
        change.physical_execution = None
        completed_changes.append(change)

    def forbidden_followup(*_args, **_kwargs):
        pytest.fail("Replacement continued without its verified physical destination.")

    monkeypatch.setattr(module, "inspect_existing_layout", inspect_initial_layout)
    monkeypatch.setattr(
        module, "execute_ch_table_reconfiguration", acknowledge_without_destination_certificate
    )
    monkeypatch.setattr(module, "inspect_completed_physical_layout", forbidden_followup)
    monkeypatch.setattr(module, "certify_replacement_scope", forbidden_followup)

    with pytest.raises(AmbiguousSqlReplaceError, match="no verified physical destination"):
        prepare_replacement_layout(get_backend_adapter("ch"), request)

    assert inspections == [request.target_table]
    assert len(completed_changes) == 1
    assert completed_changes[0].table == request.target_table
    assert completed_changes[0].cleanup_complete is True
    assert completed_changes[0].target_pair is False
    assert completed_changes[0].physical_execution is None
    assert client.commands == []


def test_missing_policy_does_not_infer_a_desired_companion():
    prepare_replacement_layout(get_backend_adapter("ch"), SimpleNamespace(ch_creation_policy=None))


def test_external_storage_is_rejected_before_any_layout_migration(monkeypatch):
    monkeypatch.setattr(
        module, "inspect_existing_layout", lambda *_a, **_k: SimpleNamespace(owns_physical=False)
    )
    monkeypatch.setattr(
        module, "plan_ch_table_reconfiguration", lambda *_a: pytest.fail("migration")
    )
    request = SimpleNamespace(
        ch_creation_policy=target_policy(pair=True), connection=object(), target_table="db.events"
    )
    with pytest.raises(AmbiguousSqlReplaceError, match="external physical storage"):
        prepare_replacement_layout(get_backend_adapter("ch"), request)
