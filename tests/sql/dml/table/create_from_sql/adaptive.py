from __future__ import annotations

import importlib
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from analytics_toolkit.general import set_connections_path
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.layout import ExistingClickHouseLayout
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import parse_create_table
from analytics_toolkit.sql.backends.models import SourceColumn
from analytics_toolkit.sql.core.replay_spool import ReplaySpool
from analytics_toolkit.sql.dml import query_input
from analytics_toolkit.sql.dml.transfer.flow import adaptive_input
from analytics_toolkit.sql.dml.transfer.runtime.models import RowBatch
from analytics_toolkit.sql.execution.backend_options import backend_operation
from sqlglot import exp, parse_one

from tests.sql._support.assertions import strip_query_label
from tests.sql._support.ch_drop_scope import DropScopeState, _DropConnection

create = importlib.import_module("analytics_toolkit.sql.dml.table.create_table_from_sql")
policy = importlib.import_module("analytics_toolkit.sql.backends.ch.policy_ctas")
legacy = importlib.import_module("analytics_toolkit.sql.backends.ch.create_table_as")


class Client:
    def __init__(self, *, exists=False, raw=None):
        self.exists = exists
        self.raw = raw
        self.commands = []
        self.inserts = []
        self.events = []
        self.close_calls = 0

    def query(self, statement, **_kwargs):
        if self.raw is not None:
            return self.raw.query(statement, **_kwargs)
        if statement.startswith("EXISTS TABLE"):
            return SimpleNamespace(result_rows=[(int(self.exists),)])
        return SimpleNamespace(result_rows=[], column_names=["id"], column_types=["Int64"])

    def command(self, statement, **_kwargs):
        self.commands.append(statement)
        self.events.append("mutation")
        if self.raw is not None:
            return self.raw.command(statement, **_kwargs)
        if statement.startswith("CREATE"):
            self.exists = True
        elif statement.startswith("DROP"):
            self.exists = False
        return None

    def close(self):
        self.close_calls += 1


def install(monkeypatch, *, native="Int64", rows=((1,),), exists=False, client=None):
    # Use only the autouse fixture's disposable connection file.
    path = Path(".connections")
    config = json.loads(path.read_text())
    config["ch"]["transfer_staging_schema"] = None
    if client is not None and client.raw is not None:
        config["ch"].update(host="coordinator", database="analytics", cluster_routing=None)
        config["ch"]["ddl_defaults"]["regular"]["shard"]["on_cluster"] = None
    path.write_text(json.dumps(config))
    set_connections_path(path)
    client = client if client is not None else Client(exists=exists)
    monkeypatch.setattr(create, "get_sql_connection", lambda _key: client)
    monkeypatch.setattr(legacy, "get_sql_connection", lambda _key: client)
    monkeypatch.setattr(get_backend_adapter("ch"), "after_create_table", lambda *_a, **_k: None)
    monkeypatch.setattr(
        query_input, "inspect_source_query_schema", lambda *_a: [SourceColumn("id", native)]
    )

    def read(*_args, **_kwargs):
        client.events.append("scan")
        for row in rows:
            yield RowBatch(["id"], [row])
        client.events.append("certified")

    def insert(_backend, _ref, _table, _columns, values, **_kwargs):
        materialized = list(values)
        client.inserts.append(materialized)
        client.events.append("insert")
        if client.raw is not None:
            client.raw.insert(_table, materialized, _columns)
        return len(materialized)

    monkeypatch.setattr(adaptive_input, "iter_source_batches", read)
    monkeypatch.setattr(query_input, "insert_rows_batch", insert)
    return client


def _existing_pair_client(rows):
    state = DropScopeState()
    state.clusters["storage"] = (state.coordinator,)
    for table in ("analytics.events", "analytics.events_shard"):
        state.host_tables[state.coordinator].pop(table)
    shard = parse_create_table(
        "CREATE TABLE analytics.events_shard (id Int64) ENGINE=MergeTree ORDER BY id",
        "analytics.events_shard",
    )
    facade = parse_create_table(
        "CREATE TABLE analytics.events (id Int64) "
        "ENGINE=Distributed('storage', 'analytics', 'events_shard', rand())",
        "analytics.events",
    )
    state._create(state.coordinator, shard, rows)
    state._create(state.coordinator, facade)
    return Client(exists=True, raw=_DropConnection(state, state.coordinator, native=False))


def run(*, schema=None, **kwargs):
    return create.create_table_from_sql(
        "ch",
        "analytics.events",
        "SELECT id FROM analytics.source",
        table_schema=schema,
        order_by="id",
        ch_distributed_table=False,
        ch_engine="MergeTree",
        retry_cnt=2,
        timeout_increment=0,
        return_metadata=True,
        **kwargs,
    )


def test_same_alias_query_checks_complete_authoritative_input_before_create(monkeypatch):
    client = install(monkeypatch, rows=((1,), (127,)))
    result = run(schema={"id": "Int8"})
    assert client.inserts == [[(1,), (127,)]]
    assert client.events.index("certified") < client.events.index("mutation")
    assert result.metadata.input_count == 2
    assert result.plan.options["input_frozen"] is True


def test_same_alias_query_last_value_rejects_before_any_target_mutation(monkeypatch):
    client = install(monkeypatch, rows=((1,), (128,)))
    with pytest.raises(ValueError, match="authoritative integer"):
        run(schema={"id": "Int8"})
    assert client.commands == []
    assert client.inserts == []


def test_same_alias_query_declared_domain_fit_keeps_server_copy(monkeypatch):
    client = install(monkeypatch)
    run(schema={"id": "Int64"})
    assert "scan" not in client.events
    assert client.commands[-1].startswith("INSERT INTO analytics.events")
    assert not client.inserts


@pytest.mark.parametrize("source", ["analytics.events", "analytics.events_shard"])
def test_query_replacement_freezes_logical_and_verified_physical_self_sources(monkeypatch, source):
    client = install(
        monkeypatch,
        rows=((1,), (2,)),
        client=_existing_pair_client(((1,), (2,))),
    )
    state = client.raw.state
    remote_before = state.snapshot()[state.remote]
    # Source bindings, exact profiling and native ownership certification stay active.
    result = create.create_table_from_sql(
        "ch",
        "analytics.events",
        f"SELECT id FROM {source}",
        order_by="id",
        ch_distributed_table=False,
        ch_engine="MergeTree",
        drop_target_if_exists=True,
        return_metadata=True,
        retry_cnt=1,
    )
    assert client.events.index("certified") < client.events.index("mutation")
    assert client.inserts == [[(1,), (2,)]]
    assert all(not statement.startswith("INSERT") for statement in client.commands)
    assert result.metadata.input_count == 2
    assert result.plan.options["input_frozen"] is True
    assert state.rows(state.coordinator, "analytics.events") == [(1,), (2,)]
    assert "analytics.events_shard" not in state.host_tables[state.coordinator]
    assert state.snapshot()[state.remote] == remote_before
    drops = [
        parse_one(strip_query_label(statement), read=ClickHouseDialect)
        for statement in client.commands
        if strip_query_label(statement).startswith("DROP TABLE")
    ]
    assert all(isinstance(drop, exp.Drop) for drop in drops)
    resources = [
        (target.name, drop.find(exp.OnCluster).this.name if drop.find(exp.OnCluster) else None)
        for drop in drops
        for target in drop.args.get("tables") or [drop.this]
    ]
    assert len(resources) == 2
    assert state.clusters["storage"] == (state.coordinator,)
    assert set(resources) == {("events", "storage"), ("events_shard", "storage")}


def test_same_alias_query_dry_plan_marks_validation_without_scanning(monkeypatch):
    client = install(monkeypatch, rows=((128,),))
    plan = run(schema={"id": "Int8"}, dry_run=True)
    assert plan.options["pending_input_validation"] is True
    assert client.events == []
    assert client.commands == []


def test_certified_query_retry_does_not_reopen_or_requery_mutable_source(monkeypatch):
    client = install(monkeypatch, rows=((1,), (127,)), exists=True)
    calls = []
    original = query_input.inspect_source_query_schema

    def inspect(*args):
        assert not calls, "certified retry inspected the original source again"
        calls.append("source")
        return original(*args)

    monkeypatch.setattr(query_input, "inspect_source_query_schema", inspect)
    layout = ExistingClickHouseLayout(
        "analytics.events", "analytics.events", False, None, None, None, "", ""
    )
    monkeypatch.setattr(policy, "inspect_existing_layout", lambda *_a, **_k: layout)
    attempts = []

    def preflight(*_args):
        attempts.append(True)
        if len(attempts) == 1:
            message = "transient target failure before any structural submission"
            raise ConnectionError(message)

    monkeypatch.setattr(get_backend_adapter("ch"), "preflight_create_table_replacement", preflight)
    result = run(schema={"id": "Int8"}, ch_replace_table=True)
    assert len(attempts) == 2
    assert calls == ["source"]
    assert client.events.count("scan") == 1
    assert client.inserts == [[(1,), (127,)]]
    assert result.metadata.input_count == 2


def test_ambiguous_query_creation_retains_certified_input_and_releases_guards(monkeypatch):
    client = install(monkeypatch, rows=((1,), (127,)))
    original_retain = ReplaySpool.retain
    manifests = []

    def retain(spool, metadata):
        result = original_retain(spool, metadata)
        manifests.append(Path(result))
        return result

    def command(statement, **_kwargs):
        client.commands.append(statement)
        message = "lost CREATE acknowledgement"
        raise ConnectionError(message)

    monkeypatch.setattr(ReplaySpool, "retain", retain)
    monkeypatch.setattr(client, "command", command)
    try:
        with backend_operation() as operation:
            with pytest.raises(ConnectionError, match="lost CREATE"):
                run(schema={"id": "Int8"})
            assert not operation.resources.get("connection_configuration_guards")
        assert client.events.count("scan") == 1
        assert len(client.commands) == 1
        assert len(manifests) == 1
        manifest = json.loads(manifests[0].read_text())
        assert manifest["row_count"] == 2
        assert manifest["metadata"]["target_schema"] == {"id": "Int8"}
        assert (manifests[0].parent / manifest["data_file"]).exists()
    finally:
        for manifest_path in manifests:
            shutil.rmtree(manifest_path.parent)


@pytest.mark.parametrize(
    ("target_type", "rows"),
    [("FixedString(3)", (("ok",), ("éé",))), ("Enum8('ok'=1)", (("ok",), ("bad",)))],
)
def test_query_preflight_checks_every_fixed_string_and_enum_value(monkeypatch, target_type, rows):
    client = install(monkeypatch, native="String", rows=rows)
    with pytest.raises(ValueError, match=r"resolved destination|authoritative|FixedString|Enum"):
        run(schema={"id": target_type})
    assert not client.commands
    assert not client.inserts
