from __future__ import annotations

from tests.sql._support.reconfigure import (
    SimpleNamespace,
    pytest,
    reconfigure_backend,
)


def test_wait_and_best_effort_cleanup_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        reconfigure_backend,
        "_wait_for_ch_table",
        lambda _connection, table: calls.append(("local", table)),
    )
    monkeypatch.setattr(
        reconfigure_backend,
        "_wait_for_ch_table_on_cluster",
        lambda _connection, table, *, ch_cluster: calls.append((ch_cluster, table)),
    )
    monkeypatch.setattr(
        reconfigure_backend,
        "_wait_for_ch_table_absence",
        lambda _connection, table: calls.append(("absent", table)),
    )
    monkeypatch.setattr(
        reconfigure_backend,
        "_wait_for_ch_table_absence_on_cluster",
        lambda _connection, table, *, ch_cluster: calls.append((f"absent:{ch_cluster}", table)),
    )
    reconfiguration = SimpleNamespace(
        replacement_table="analytics.wrapper_tmp",
        strategy="cross_cluster_rebuild",
        temporary_tables=["analytics.wrapper_tmp", "analytics.events_shard"],
        target_cluster="archive",
        source_cluster="core",
        cleanup_tables=[("analytics.local_tmp", None), ("analytics.cluster_tmp", "core")],
    )

    reconfigure_backend._wait_for_created_replacement(None, reconfiguration)
    reconfigure_backend._wait_for_cleanup(None, reconfiguration)
    reconfiguration.strategy = "local_rebuild"
    reconfigure_backend._wait_for_created_replacement(None, reconfiguration)
    reconfiguration.strategy = "cross_cluster_rebuild"
    reconfiguration.target_cluster = None
    reconfigure_backend._wait_for_created_replacement(None, reconfiguration)
    reconfiguration.replacement_table = None
    reconfigure_backend._wait_for_created_replacement(None, reconfiguration)
    reconfiguration.temporary_table_scopes = [
        ("analytics.explicit_local", None),
        ("analytics.explicit_cluster", "archive"),
    ]
    reconfigure_backend._wait_for_created_replacement(None, reconfiguration)

    class CleanupAdapter:
        def __init__(self) -> None:
            self.commands: list[str] = []

        def execute_command(self, _connection: object, sql_text: str) -> None:
            self.commands.append(sql_text)
            if len(self.commands) == 1:
                message = "best effort"
                raise RuntimeError(message)

    cleanup_adapter = CleanupAdapter()
    reconfigure_backend._best_effort_cleanup(cleanup_adapter, None, reconfiguration)

    assert ("archive", "analytics.events_shard") in calls
    assert ("absent", "analytics.local_tmp") in calls
    assert ("absent:core", "analytics.cluster_tmp") in calls
    assert len(cleanup_adapter.commands) == 2
