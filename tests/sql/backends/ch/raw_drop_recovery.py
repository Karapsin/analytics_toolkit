"""Default raw recovery transports need their own structural acknowledgement guard."""

from __future__ import annotations

from itertools import count
from types import SimpleNamespace

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch import drop_execution
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.outage_context import INTERNAL_IO
from analytics_toolkit.sql.connection.errors import AmbiguousSqlReplaceError
from analytics_toolkit.sql.dml.transfer.runtime.retry import is_non_retryable_sql_error
from analytics_toolkit.sql.execution.backend_options import backend_operation
from sqlglot import exp, parse_one

from tests.sql._support.ch_drop_scope import _DropConnection, install_drop_scope


def test_public_drop_default_raw_recovery_retains_lost_acknowledgement_identity(
    monkeypatch, write_sql_connections
):
    state = install_drop_scope(monkeypatch, write_sql_connections, driver="native")
    ticks = count(0, 2)
    monkeypatch.setattr(drop_execution, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    state.delay_cluster_drops.add(("storage", "analytics.events_shard"))
    resource_uuid = state.host_tables[state.remote]["analytics.events_shard"].uuid
    submitted = []
    failure = ConnectionError("A raw recovery DROP completed without its acknowledgement")

    class LostAcknowledgement(_DropConnection):
        def __init__(self, *args, **options):
            super().__init__(*args, **options)
            self.raw_recovery = INTERNAL_IO.get()

        def command(self, statement, settings=None):
            result = super().command(statement, settings=settings)
            tree = parse_one(statement, read=ClickHouseDialect)
            if self.raw_recovery and isinstance(tree, exp.Drop):
                submitted.append((self.host, statement))
                raise failure
            return result

    state.connection_class = LostAcknowledgement

    with backend_operation() as operation:
        with pytest.raises(AmbiguousSqlReplaceError) as caught:
            sql.drop_tables(
                "ch",
                "analytics.events",
                ch_cluster="storage",
                if_exists=True,
                ch_wait_for_absence=True,
                ch_wait_timeout_seconds=1,
                ch_wait_poll_interval_seconds=1,
                ch_retry_per_host_drops=True,
            )
        assert operation.structural_replay_blocked is True
        assert operation.preserve_recovery is True
        assert is_non_retryable_sql_error(caught.value)

    assert caught.value.__cause__ is failure
    assert len(submitted) == 1
    assert submitted[0][0] == state.remote
    assert '"analytics"."events_shard"' in str(caught.value)
    assert state.host_endpoints[state.remote][1] in str(caught.value)
    assert "9000" in str(caught.value)
    journal = caught.value.analytics_toolkit_sql_drop_journal
    assert journal["endpoint"] == f"{state.host_endpoints[state.remote][1]}:9000"
    assert (str(journal["server_identity"][0]), journal["server_identity"][1]) == (
        state.server_identities[state.remote]
    )
    assert operation.resources["clickhouse_drop_recovery_journal"] == [journal]
    assert journal["submitted"] is True
    assert journal["acknowledged"] is False
    assert journal["recovery_retained"] is True
    assert journal["certificate"][0:2] == ("analytics", "events_shard")
    assert journal["certificate"][-2] == resource_uuid
    assert "analytics.events_shard" not in state.host_tables[state.remote]
    assert state.host_tables[state.coordinator]["analytics.events_shard"].rows == state.foreign_rows
    assert all(client.close_calls == 1 for client in state.opened)
