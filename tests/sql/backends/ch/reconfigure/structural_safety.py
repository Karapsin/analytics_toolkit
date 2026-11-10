from __future__ import annotations

import pytest
from analytics_toolkit.sql.backends.ch.cutover import StructuralCutoverError, execute_cutover
from analytics_toolkit.sql.backends.ch.layout import inspect_existing_layout
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import (
    parse_create_table,
    retarget_create,
    transform_distributed_create,
)
from analytics_toolkit.sql.backends.ch.reconfigure_topology import (
    DestinationTopology,
    validate_destination,
)

from tests.sql._support.reconfigure import (
    SHARD_DDL,
    CountingReconfigureClient,
    FakeClickHouseResult,
    InvalidSqlInputError,
    LocalReconfigureClient,
    ReconfigureClient,
    _options,
    _regular_defaults,
    get_backend_adapter,
    plan_ch_table_reconfiguration,
    reconfigure_backend,
)


def test_local_layout_never_owns_a_suffix_companion() -> None:
    client = LocalReconfigureClient()
    layout = inspect_existing_layout(client, "analytics.local_events")
    assert layout.logical_table == layout.physical_table == "analytics.local_events"
    assert layout.create_distributed_pair is False
    assert not any("local_events_shard" in sql for sql in client.queries)


def test_managed_layout_verifies_referenced_storage() -> None:
    class QuotedClient(ReconfigureClient):
        def query(self, sql: str) -> FakeClickHouseResult:
            return super().query(sql.replace("`", ""))

    client = QuotedClient()
    layout = inspect_existing_layout(client, "analytics.events")
    assert layout.create_distributed_pair is True
    assert layout.physical_table == "`analytics`.`events_shard`"
    assert layout.distributed_cluster == "{cluster}"
    assert layout.owns_physical is True


def test_custom_physical_reference_is_available_for_data_operations_without_owning_its_storage():
    class ExternalClient(ReconfigureClient):
        def query(self, sql: str) -> FakeClickHouseResult:
            if sql == "SHOW CREATE TABLE analytics.events":
                return FakeClickHouseResult(
                    [
                        (
                            "CREATE TABLE analytics.events (id UInt64) ENGINE="
                            "Distributed('core', 'external', 'shared_storage', rand())",
                        )
                    ]
                )
            if sql.startswith("SELECT create_table_query FROM clusterAllReplicas("):
                return FakeClickHouseResult(
                    [
                        (
                            "CREATE TABLE external.shared_storage (id UInt64) "
                            "ENGINE=MergeTree ORDER BY id",
                        )
                    ]
                )
            return super().query(sql)

    layout = inspect_existing_layout(ExternalClient(), "analytics.events")
    assert layout.physical_table == "`external`.`shared_storage`"
    assert layout.create_distributed_pair is True
    assert layout.owns_physical is False


def test_replacement_create_is_plain_and_facade_excludes_physical_properties() -> None:
    create = parse_create_table(SHARD_DDL, "analytics.events_shard")
    replacement = retarget_create(create, "analytics.replacement", None)
    assert replacement.args.get("exists") is False
    facade = transform_distributed_create(
        create,
        table_name="analytics.events",
        execution_cluster="facades",
        target_cluster="storage",
        shard_table="analytics.events_shard",
        ch_sharding_key="randCanonical()",
        ch_distributed_engine_template=(
            "Distributed({cluster}, {database}, {shard_table}, {sharding_key}, 'policy')"
        ),
    ).sql(dialect="clickhouse")
    assert (
        "Distributed('storage', 'analytics', 'events_shard', randCanonical(), 'policy')" in facade
    )
    for physical_clause in ("ORDER BY", "PARTITION BY", "SETTINGS", "INDEX"):
        assert physical_clause not in facade


def test_lost_cutover_ack_never_runs_an_unacknowledged_inverse() -> None:
    client = ReconfigureClient(database_engine="Ordinary")
    adapter = get_backend_adapter("ch")
    change = plan_ch_table_reconfiguration(adapter, client, _options(ch_engine="MergeTree"))
    submitted = []
    error = ConnectionError("lost acknowledgement")

    def command(sql: str, settings: object = None) -> None:
        submitted.append(sql)
        if len(submitted) == 2:
            raise error

    client.command = command
    with pytest.raises(StructuralCutoverError, match="Retained recovery resources") as caught:
        execute_cutover(adapter, client, change)
    assert caught.value.__cause__ is error
    assert caught.value.analytics_toolkit_sql_retry_safe is False
    assert submitted == change.cutover_sqls[:2]
    assert [step["acknowledged"] for step in change.cutover_journal] == [True, False]


def test_local_to_pair_rejects_a_preexisting_companion_before_ddl() -> None:
    class CollisionClient(LocalReconfigureClient):
        def query(self, sql: str) -> FakeClickHouseResult:
            if "clusterAllReplicas('core', system, tables)" in sql:
                self.queries.append(sql)
                return FakeClickHouseResult([(1,)])
            return super().query(sql)

    client = CollisionClient()
    with pytest.raises(InvalidSqlInputError, match="already contains"):
        plan_ch_table_reconfiguration(
            get_backend_adapter("ch"),
            client,
            _options(
                table="analytics.local_events",
                ch_distributed_table=True,
                regular_defaults=_regular_defaults(),
            ),
        )
    assert client.commands == []


def test_relocation_rejects_copy_routing_to_original_storage() -> None:
    client = ReconfigureClient()
    with pytest.raises(InvalidSqlInputError, match="replacement storage endpoints"):
        validate_destination(
            client,
            DestinationTopology(
                source_pair=True,
                target_pair=True,
                source_cluster="core",
                target_cluster="archive",
                distributed_cluster="core",
                desired_shard_table="analytics.events_shard",
                cross_cluster=True,
            ),
        )
    assert client.commands == []


def test_count_failure_rolls_back_acknowledged_cutovers_in_reverse_order(monkeypatch) -> None:
    client = CountingReconfigureClient([3, 3, 3, 99])
    adapter = get_backend_adapter("ch")
    change = plan_ch_table_reconfiguration(adapter, client, _options(ch_engine="MergeTree"))
    monkeypatch.setattr(reconfigure_backend, "_wait_for_created_replacement", lambda *_: None)
    with pytest.raises(RuntimeError, match="row count changed during cutover"):
        reconfigure_backend.execute_ch_table_reconfiguration(
            adapter, client, change, validate_row_count=True
        )
    assert all(step.get("rollback_acknowledged") for step in change.cutover_journal)
    inverses = [step["inverse"] for step in reversed(change.cutover_journal)]
    cutovers = [sql for sql in client.commands if sql.startswith(("RENAME", "EXCHANGE"))]
    assert cutovers == change.cutover_sqls + inverses


def test_structural_execution_failure_retains_artifacts_without_cleanup_or_rollback(monkeypatch):
    client = CountingReconfigureClient([3, 3, 3])
    adapter = get_backend_adapter("ch")
    change = plan_ch_table_reconfiguration(adapter, client, _options(ch_engine="MergeTree"))
    failure = StructuralCutoverError("uncertain structural submission")
    monkeypatch.setattr(reconfigure_backend, "_wait_for_created_replacement", lambda *_a: None)

    def fail(*_args):
        raise failure

    monkeypatch.setattr(reconfigure_backend, "execute_cutover", fail)
    monkeypatch.setattr(
        reconfigure_backend, "rollback_cutover", lambda *_a: pytest.fail("rollback")
    )
    monkeypatch.setattr(
        reconfigure_backend, "_best_effort_cleanup", lambda *_a: pytest.fail("cleanup")
    )
    with pytest.raises(StructuralCutoverError, match="uncertain structural") as caught:
        reconfigure_backend.execute_ch_table_reconfiguration(
            adapter, client, change, validate_row_count=True
        )
    assert caught.value is failure
    assert change.owned_table_scopes
    assert change.cleanup_complete is False
