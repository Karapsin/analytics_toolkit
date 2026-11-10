from __future__ import annotations

from dataclasses import replace

import pandas as pd
import pytest
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.creation_policy import ClickHouseCreationPolicy
from analytics_toolkit.sql.backends.ch.replacement_resources import create_replacement_physical
from analytics_toolkit.sql.backends.models import StageFinalizationRequest
from analytics_toolkit.sql.connection.errors import AmbiguousSqlReplaceError
from analytics_toolkit.sql.execution.backend_options import backend_operation

from tests.sql._support.ch_replace import PHYSICAL, STAGE, TARGET, ReplacementClient, Table
from tests.sql._support.reconfigure import importlib

module = importlib.import_module("analytics_toolkit.sql.backends.ch.replacement_resources")


def request_for(client):
    return StageFinalizationRequest(
        connection=client,
        connection_key="ch",
        stage_table=STAGE,
        target_table=TARGET,
        replace_target_table=True,
        target_exists=True,
        sample_batch=pd.DataFrame({"id": [1], "value": ["new"]}),
        target_column_types={"id": "Int64", "value": "String"},
        insert_column_types={"id": "Int64", "value": "String"},
        ch_creation_policy=ClickHouseCreationPolicy(
            create_distributed_pair=True,
            shard_engine="ReplicatedMergeTree",
            shard_on_cluster="core",
            distributed_engine_template="Distributed({cluster},{database},{shard_table},{sharding_key})",
            distributed_cluster="core",
            distributed_on_cluster="core",
            sharding_key="id",
            ddl_ready_timeout_seconds=1,
            ddl_wait_policy="wait_all",
        ),
    )


def test_physical_acknowledgement_establishes_owned_scope_before_readiness(monkeypatch):
    client = ReplacementClient()
    owned = []
    error = TimeoutError("not ready yet")

    def wait(*_args, **_kwargs):
        assert owned == [("sandbox.candidate", "core")]
        raise error

    monkeypatch.setattr(module, "_wait_for_ch_table_on_cluster", wait)
    with pytest.raises(TimeoutError) as caught:
        create_replacement_physical(
            get_backend_adapter("ch"), request_for(client), "sandbox.candidate", owned.append
        )
    assert caught.value is error
    assert client.tables["core", PHYSICAL].rows == 17
    assert len(client.commands) == 1
    assert "IF NOT EXISTS" not in client.commands[0][0]
    assert client.commands[0][1] == {
        "distributed_ddl_task_timeout": 300,
        "distributed_ddl_output_mode": "throw_only_active",
    }


def test_guarded_cleanup_drops_only_acknowledged_resources_after_readiness_failure(monkeypatch):
    client = ReplacementClient()
    error = TimeoutError("not ready")

    def wait(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(module, "_wait_for_ch_table_on_cluster", wait)
    original = dict(client.tables)
    with pytest.raises(TimeoutError) as caught:
        get_backend_adapter("ch").finalize_stage_table(request_for(client))
    assert caught.value is error
    assert client.tables == original
    drops = [sql for sql, _ in client.commands if sql.startswith("DROP")]
    assert len(drops) == 1
    assert "__replace_guarded_" in drops[0]
    assert "ON CLUSTER core" in drops[0]


@pytest.mark.parametrize("after_apply", [False, True], ids=["collision", "lost-ack"])
def test_unacknowledged_create_never_owns_or_drops_the_candidate(after_apply):
    error = ConnectionError("CREATE acknowledgement lost")

    class Client(ReplacementClient):
        def command(self, sql, settings=None):
            if after_apply:
                super().command(sql, settings)
            raise error

    client = Client()
    candidate = "sandbox.candidate"
    if not after_apply:
        client.tables["core", candidate] = Table(100, ("unrelated",))
    owned = []
    with backend_operation() as state:
        with pytest.raises(AmbiguousSqlReplaceError, match="without claiming ownership") as caught:
            create_replacement_physical(
                get_backend_adapter("ch"), request_for(client), candidate, owned.append
            )
        assert state.structural_replay_blocked is True
    assert caught.value.__cause__ is error
    assert owned == []
    assert client.tables["core", candidate].rows == (0 if after_apply else 100)
    assert not any("DROP" in sql for sql, _ in client.commands)


@pytest.mark.parametrize("wait_policy", ["wait_none", "wait_all"])
def test_local_replacement_can_be_created_without_ownership_callback(monkeypatch, wait_policy):
    client = ReplacementClient()
    request = request_for(client)
    request = replace(
        request,
        ch_creation_policy=replace(
            request.ch_creation_policy, shard_on_cluster=None, ddl_wait_policy=wait_policy
        ),
    )
    waited = []
    monkeypatch.setattr(module, "_wait_for_ch_table", lambda _c, table, **_k: waited.append(table))
    create_replacement_physical(get_backend_adapter("ch"), request, "sandbox.local_candidate")
    assert waited == ([] if wait_policy == "wait_none" else ["sandbox.local_candidate"])
    assert ("shard5", "sandbox.local_candidate") in client.tables
    assert all("ON CLUSTER" not in sql for sql, _ in client.commands)
    assert any("CREATE TABLE sandbox.local_candidate" in sql for sql, _ in client.commands)


def test_unacknowledged_create_without_operation_state_keeps_candidate_and_original_error():
    failure = OSError("no acknowledgement")

    class Client(ReplacementClient):
        def command(self, *_args, **_kwargs):
            raise failure

    client = Client()
    with pytest.raises(AmbiguousSqlReplaceError) as caught:
        create_replacement_physical(
            get_backend_adapter("ch"), request_for(client), "sandbox.candidate"
        )
    assert caught.value.__cause__ is failure
    assert "sandbox.candidate" not in [name for _scope, name in client.tables]
