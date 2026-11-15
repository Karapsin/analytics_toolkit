from __future__ import annotations

from types import SimpleNamespace

import pytest
from analytics_toolkit.sql.backends.ch.cutover import StructuralCutoverError, execute_cutover
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.layout import inspect_existing_layout
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import (
    engine_name,
    parse_create_table,
    retarget_create,
    transform_distributed_create,
)
from analytics_toolkit.sql.backends.ch.reconfigure_topology import (
    DestinationTopology,
    validate_destination,
)
from sqlglot import exp

from tests.sql._support.reconfigure import (
    SHARD_DDL,
    TABLE_DDL,
    CountingReconfigureClient,
    FakeClickHouseResult,
    InvalidSqlInputError,
    LocalReconfigureClient,
    ReconfigureClient,
    _options,
    _regular_defaults,
    get_backend_adapter,
    plan_ch_table_reconfiguration,
    reconfigure_api,
    reconfigure_backend,
    sql,
)

_TTL_COLUMNS = (
    "(id UInt64 DEFAULT 7 COMMENT 'id TTL stays text', "
    "ts DateTime('UTC') DEFAULT toDateTime('2099-01-01 00:00:00', 'UTC') "
    "TTL ts + INTERVAL 1 DAY COMMENT 'ts comment')"
)
_LOCAL_TTL_DDL = (
    f"CREATE TABLE analytics.local_events {_TTL_COLUMNS} ENGINE=MergeTree "
    "PARTITION BY toYYYYMM(ts) ORDER BY id TTL ts + INTERVAL 2 DAY"
)
_LEGACY_FACADE_TTL_DDL = (
    f"CREATE TABLE analytics.events {_TTL_COLUMNS[:-1]}, "
    "CONSTRAINT positive_id CHECK id > 0) "
    "ENGINE=Distributed('core', 'analytics', 'events_shard', cityHash64(id))"
)


def _non_ttl_columns(create):
    return {
        column.name: (
            column.kind.sql(dialect=ClickHouseDialect),
            [
                constraint.sql(dialect=ClickHouseDialect)
                for constraint in column.constraints
                if not isinstance(constraint.kind, exp.MergeTreeTTL)
            ],
        )
        for column in create.this.expressions
        if isinstance(column, exp.ColumnDef)
    }


@pytest.mark.parametrize("dry_run", [False, True])
def test_local_to_pair_reconfiguration_removes_only_facade_column_ttl(monkeypatch, dry_run):
    class ColumnTTLClient(LocalReconfigureClient):
        def query(self, statement, settings=None):
            if statement == "SHOW CREATE TABLE analytics.local_events":
                self.queries.append(statement)
                self.metadata_overrides[("analytics", "local_events")] = _LOCAL_TTL_DDL
                return FakeClickHouseResult([(_LOCAL_TTL_DDL,)])
            return super().query(statement, settings=settings)

        def command(self, statement, settings=None):
            if statement.startswith("CREATE TABLE"):
                create = parse_create_table(statement, "replacement")
                if engine_name(create) == "Distributed" and create.find(exp.MergeTreeTTL):
                    message = "Distributed engine rejects column TTL on new table creation"
                    raise ValueError(message)
            return super().command(statement, settings)

    client = ColumnTTLClient()
    monkeypatch.setattr(
        reconfigure_api,
        "get_connection_config",
        lambda _key: SimpleNamespace(
            connection_key="ch",
            backend="ch",
            ddl_defaults=SimpleNamespace(regular=_regular_defaults()),
        ),
    )
    monkeypatch.setattr(reconfigure_api, "get_sql_connection", lambda _key: client)
    monkeypatch.setattr(reconfigure_backend, "_wait_for_created_replacement", lambda *_a: None)
    monkeypatch.setattr(reconfigure_backend, "_wait_for_cleanup", lambda *_a: None)
    result = sql.ch_reconfigure_table(
        "ch",
        "analytics.local_events",
        ch_distributed_table=True,
        retry_cnt=1,
        timeout_increment=0,
        dry_run=dry_run,
    )
    statements = result.sqls if dry_run else client.commands
    creates = [
        parse_create_table(statement, "replacement")
        for statement in statements
        if statement.startswith("CREATE TABLE")
    ]
    facade = next(create for create in creates if engine_name(create) == "Distributed")
    physical = next(create for create in creates if engine_name(create).endswith("MergeTree"))
    original = parse_create_table(_LOCAL_TTL_DDL, "analytics.local_events")
    assert not list(facade.find_all(exp.MergeTreeTTL))
    assert len(list(physical.find_all(exp.MergeTreeTTL))) == 2
    assert _non_ttl_columns(facade) == _non_ttl_columns(physical) == _non_ttl_columns(original)
    assert "id TTL stays text" in facade.sql(dialect=ClickHouseDialect)
    assert client.closed


@pytest.mark.parametrize("ddl", [_LOCAL_TTL_DDL, _LEGACY_FACADE_TTL_DDL])
def test_new_facade_removes_column_ttl_without_mutating_source_ast(ddl):
    create = parse_create_table(ddl, "source")
    before = create.sql(dialect=ClickHouseDialect)
    facade = transform_distributed_create(
        create,
        table_name="analytics.output",
        execution_cluster="facades",
        target_cluster="storage",
        shard_table="analytics.output_shard",
        ch_sharding_key="cityHash64(id)",
        ch_distributed_engine_template=(
            "Distributed({cluster}, {database}, {shard_table}, {sharding_key})"
        ),
    )
    assert not list(facade.find_all(exp.MergeTreeTTL))
    assert _non_ttl_columns(facade) == _non_ttl_columns(create)
    assert [
        item.sql(dialect=ClickHouseDialect)
        for item in facade.this.expressions
        if not isinstance(item, exp.ColumnDef)
    ] == [
        item.sql(dialect=ClickHouseDialect)
        for item in create.this.expressions
        if not isinstance(item, exp.ColumnDef)
    ]
    assert create.sql(dialect=ClickHouseDialect) == before
    assert create.find(exp.MergeTreeTTL) is not None


@pytest.mark.parametrize("difference", [None, "default", "type", "comment"])
def test_managed_pair_identity_excludes_only_physical_column_ttl(difference):
    facade = (
        "CREATE TABLE analytics.events (id UInt64 DEFAULT 7 COMMENT 'id TTL stays text', "
        "ts DateTime('UTC') DEFAULT toDateTime('2099-01-01 00:00:00', 'UTC') "
        "COMMENT 'ts comment') "
        "ENGINE=Distributed('core', 'analytics', 'events_shard', cityHash64(id))"
    )
    physical = _LOCAL_TTL_DDL.replace("analytics.local_events", "analytics.events_shard")
    if difference == "default":
        physical = physical.replace("DEFAULT 7", "DEFAULT 8")
    elif difference == "type":
        physical = physical.replace("id UInt64", "id UInt32")
    elif difference == "comment":
        physical = physical.replace("id TTL stays text", "different comment")

    class TTLPairClient(ReconfigureClient):
        def query(self, statement, settings=None):
            if statement == "SHOW CREATE TABLE analytics.events":
                self.queries.append(statement)
                return FakeClickHouseResult([(facade,)])
            if statement.startswith("SELECT create_table_query FROM clusterAllReplicas("):
                self.queries.append(statement)
                return FakeClickHouseResult([(physical,)])
            return super().query(statement, settings=settings)

    layout = inspect_existing_layout(TTLPairClient(), "analytics.events")
    assert layout.create_distributed_pair
    assert layout.physical_table == "`analytics`.`events_shard`"
    assert layout.owns_physical is (difference is None)


def test_local_layout_never_owns_a_suffix_companion() -> None:
    client = LocalReconfigureClient()
    layout = inspect_existing_layout(client, "analytics.local_events")
    assert layout.logical_table == layout.physical_table == "analytics.local_events"
    assert layout.create_distributed_pair is False
    assert not any("local_events_shard" in sql for sql in client.queries)


def test_managed_layout_verifies_referenced_storage() -> None:
    class QuotedClient(ReconfigureClient):
        def query(self, sql: str, settings=None) -> FakeClickHouseResult:
            return super().query(sql.replace("`", ""), settings=settings)

    client = QuotedClient()
    layout = inspect_existing_layout(client, "analytics.events")
    assert layout.create_distributed_pair is True
    assert layout.physical_table == "`analytics`.`events_shard`"
    assert layout.distributed_cluster == "{cluster}"
    assert layout.owns_physical is True


def test_custom_physical_reference_is_available_for_data_operations_without_owning_its_storage():
    class ExternalClient(ReconfigureClient):
        def query(self, sql: str, settings=None) -> FakeClickHouseResult:
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
            return super().query(sql, settings=settings)

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
    client = LocalReconfigureClient()
    client.metadata_overrides[("analytics", "local_events_shard")] = (
        "CREATE TABLE analytics.local_events_shard (id UInt64, dt Date) "
        "ENGINE=MergeTree ORDER BY id"
    )
    client.metadata._seed()
    original = dict(client.metadata.tables)
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
    assert client.metadata.tables == original
    assert all(client.metadata.tables[identity] is create for identity, create in original.items())


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
                logical_table="analytics.events",
                facade_cluster="core",
                source_facade_ddl=TABLE_DDL,
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
