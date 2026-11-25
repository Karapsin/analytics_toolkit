"""The disposable remote-skip case registers valid exact-scope cleanup resources."""

from __future__ import annotations

import inspect

from analytics_toolkit import sql

from tests._support.paths import REPO_ROOT
from tests.sql.integration._support.resources import ResourceRegistry
from tests.sql.integration.core import audit_ch_drop_scope as scene


def test_remote_plain_skip_scenario_registers_fallback_before_execution_and_cleans_exact_scopes(
    monkeypatch, tmp_path
):
    registry = ResourceRegistry(REPO_ROOT, None, tmp_path)
    relation = "it_unit_remote_plain_skip"
    table = scene.quoted(scene._DATABASE, relation)
    rows = [(value, f"old-{value}") for value in range(100)]
    commands = []
    drops = []
    reached = []
    drop_signature = inspect.signature(sql.drop_tables)

    def execute(alias, statement, **_kwargs):
        assert alias == "ch"
        commands.append(statement)

    def execute_create(alias, target, statement, **kwargs):
        assert registry.tables == [("ch", table, None)]
        assert alias == "ch"
        assert target == table
        assert statement.startswith("CREATE TEMPORARY TABLE skipped_setup")
        assert kwargs["if_not_exists"] is True
        assert kwargs["ch_shard_on_cluster"] == scene._ARCHIVE
        reached.append(target)
        return sql.SqlOperationResult(
            rows=0, data=0, metadata=sql.SqlOperationMetadata(statement_count=0)
        )

    def drop(*args, **kwargs):
        drop_signature.bind(*args, **kwargs)
        drops.append((args, kwargs))

    monkeypatch.setattr(scene, "resource_name", lambda _purpose: relation)
    monkeypatch.setattr(scene, "seed_storage", lambda *_a, **_kw: rows)
    monkeypatch.setattr(
        scene,
        "inventory",
        lambda cluster, _databases, _prefix: [] if cluster == scene._CORE else [("owned",)],
    )
    monkeypatch.setattr(scene, "stored_rows", lambda *_args: rows)
    monkeypatch.setattr(sql, "execute", execute)
    monkeypatch.setattr(sql, "execute_create", execute_create)
    monkeypatch.setattr(sql, "drop_tables", drop)

    scene.test_execute_create_skips_existing_remote_plain_target_before_setup(registry)

    assert reached == [table]
    assert len(commands) == 1
    assert commands[0].startswith(f"CREATE TABLE {table} ON CLUSTER")
    assert registry.cleanup() == []
    assert len(commands) == 2
    assert (
        commands[1]
        == f"DROP TABLE IF EXISTS {table} ON CLUSTER {scene.quoted(scene._ARCHIVE)} SYNC"
    )
    assert drops == [(("ch", table), {"if_exists": True, "ch_cluster": None})]
