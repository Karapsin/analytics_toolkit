"""A distributed CREATE collision cannot authorize abandoning partial resources."""

from __future__ import annotations

import pandas as pd
import pytest
from analytics_toolkit import general, sql
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from sqlglot import exp, parse_one

from tests.sql._support.load_client_validation import client_target


class _RemoteStageCollisionError(RuntimeError):
    code = 57
    error_name = "TABLE_ALREADY_EXISTS"


class _UnexpectedStageRetryError(RuntimeError):
    pass


@pytest.mark.parametrize("transport", ["http", "native"])
def test_public_distributed_stage_collision_retains_the_original_uncertain_candidate(
    monkeypatch, write_sql_connections, transport
):
    path = write_sql_connections(
        {
            "ch": {
                "type": "ch",
                "driver": transport,
                "host": "fake.invalid",
                "port": 9000 if transport == "native" else 8123,
                "database": "sandbox",
                "user": "fake",
                "password": "fake",
                "ddl_defaults": {
                    "regular": {
                        "create_distributed_pair": False,
                        "shard": {"engine": "MergeTree", "on_cluster": None},
                    },
                    "staging": {
                        "create_distributed_pair": False,
                        "shard": {"engine": "MergeTree", "on_cluster": "core"},
                    },
                },
            }
        }
    )
    general.set_connections_path(path)
    store = client_target(monkeypatch, transport, {"payload": "Int64"})
    original_target = [dict(row) for row in store.rows]
    creations = []
    endpoints = {}
    execute = store.execute

    def execute_boundary(statement, params=None):
        tree = parse_one(statement, read=ClickHouseDialect)
        if isinstance(tree, exp.Create):
            assert isinstance(tree.this, exp.Schema)
            table = tree.this.this
            if table.name != "events":
                cluster = tree.find(exp.OnCluster)
                assert cluster is not None
                assert cluster.this.name == "core"
                identity = table.db, table.name
                creations.append(identity)
                if len(creations) > 1:
                    message = "An ambiguous distributed creation was retried under another name."
                    raise _UnexpectedStageRetryError(message)
                # The external race occurs after absence preflight: the first
                # host creates this operation's table, while the second host's
                # independent matching name makes the cluster statement fail.
                endpoints["first", identity] = []
                endpoints["second", identity] = [{"payload": 999}]
                message = "A peer already owns the stage name."
                raise _RemoteStageCollisionError(message)
        return execute(statement, params)

    monkeypatch.setattr(store, "execute", execute_boundary)

    with pytest.raises((_RemoteStageCollisionError, _UnexpectedStageRetryError)) as raised:
        sql.load_df(
            "ch",
            "sandbox.events",
            pd.DataFrame({"payload": [1]}),
            table_schema={"payload": "Int64"},
            write_mode="append",
            key_columns="payload",
            retry_cnt=1,
            timeout_increment=0,
        )

    assert len(creations) == 1
    candidate = creations[0]
    assert endpoints == {("first", candidate): [], ("second", candidate): [{"payload": 999}]}
    retained = (
        parse_one(
            "SELECT * FROM " + raised.value.analytics_toolkit_sql_stage_candidate,
            read=ClickHouseDialect,
        )
        .args["from_"]
        .this
    )
    assert (retained.db, retained.name) == candidate
    assert store.rows == original_target
    assert store.bound_rows == []
    assert not any(kind == "DROP" for kind, _statement in store.submissions)
