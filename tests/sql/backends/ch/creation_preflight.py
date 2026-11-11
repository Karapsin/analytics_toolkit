from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError, SqlConfigError

module = importlib.import_module("analytics_toolkit.sql.backends.ch.creation_preflight")


def options(**changes):
    values = {
        "ch_replace_table": True,
        "table_name": "db.events",
        "connection_key": "ch",
        "query_label": None,
        "ch_only_shard": False,
        "ch_creation_policy": SimpleNamespace(
            create_distributed_pair=True,
            shard_on_cluster="storage",
            distributed_on_cluster="facades",
        ),
    }
    values.update(changes)
    return SimpleNamespace(**values)


def layout(**changes):
    values = {
        "owns_physical": True,
        "create_distributed_pair": True,
        "shard_on_cluster": "storage",
        "distributed_on_cluster": "facades",
    }
    values.update(changes)
    return SimpleNamespace(**values)


def setup(monkeypatch, existing=None):
    calls = []
    adapter = SimpleNamespace(table_exists=lambda *_a, **_k: existing is not None)
    monkeypatch.setattr(module, "inspect_existing_layout", lambda *_a, **_k: existing)
    monkeypatch.setattr(module, "resolve_optional_cluster", lambda _c, cluster: cluster)
    monkeypatch.setattr(module, "table_exists_on_cluster", lambda *_a: False)
    monkeypatch.setattr(module, "endpoints", lambda _c, cluster: {(cluster, 9000)})
    monkeypatch.setattr(module, "prepare_replacement_layout", lambda *_a: calls.append("convert"))
    return adapter, calls


def test_replace_preflight_skips_ordinary_create_and_requires_resolved_policy():
    module.preflight_create_table_replacement(object(), object(), options(ch_replace_table=False))
    with pytest.raises(SqlConfigError, match="resolved creation policy"):
        module.preflight_create_table_replacement(
            object(), object(), options(ch_creation_policy=None)
        )


def test_external_storage_replacement_is_rejected_before_conversion(monkeypatch):
    adapter, calls = setup(monkeypatch, layout(owns_physical=False))
    with pytest.raises(InvalidSqlInputError, match="externally owned"):
        module.preflight_create_table_replacement(adapter, object(), options())
    assert calls == []


def test_same_storage_requires_explicit_migration_even_when_new_scope_is_empty(monkeypatch):
    adapter, calls = setup(monkeypatch, layout(shard_on_cluster="old"))
    with pytest.raises(InvalidSqlInputError, match="Physical placement differs"):
        module.preflight_create_table_replacement(adapter, object(), options())
    assert calls == []


@pytest.mark.parametrize("occupied", [False, True])
def test_moving_existing_facade_rejects_collision_or_requires_reconfiguration(
    monkeypatch, occupied
):
    adapter, calls = setup(monkeypatch, layout(distributed_on_cluster="old"))
    monkeypatch.setattr(module, "table_exists_on_cluster", lambda *_a: occupied)
    expected = "unowned target" if occupied else "Facade placement differs"
    with pytest.raises(InvalidSqlInputError, match=expected):
        module.preflight_create_table_replacement(adapter, object(), options())
    assert calls == []


def test_local_facade_can_move_to_empty_verified_scope(monkeypatch):
    adapter, calls = setup(monkeypatch, layout(distributed_on_cluster=None))
    module.preflight_create_table_replacement(adapter, object(), options())
    assert calls == []


def test_existing_pair_in_same_verified_scopes_requires_no_migration_or_collision_probe(
    monkeypatch,
):
    adapter, calls = setup(monkeypatch, layout())
    monkeypatch.setattr(
        module, "table_exists_on_cluster", lambda *_a: pytest.fail("unnecessary collision probe")
    )
    module.preflight_create_table_replacement(adapter, object(), options())
    assert calls == []


@pytest.mark.parametrize("existing_pair", [False, True])
def test_verified_layout_conversion_is_delegated_only_after_collision_checks(
    monkeypatch, existing_pair
):
    adapter, calls = setup(monkeypatch, layout(create_distributed_pair=existing_pair))
    request = options(ch_only_shard=existing_pair)
    module.preflight_create_table_replacement(adapter, object(), request)
    assert calls == ["convert"]


@pytest.mark.parametrize("occupied", [False, True])
def test_new_local_companion_scope_checks_named_target_without_cluster(monkeypatch, occupied):
    adapter, _calls = setup(monkeypatch)
    looked_up = []
    adapter.table_exists = lambda _c, table, **_k: looked_up.append(table) or occupied
    request = options()
    request.ch_creation_policy.shard_on_cluster = None
    if occupied:
        with pytest.raises(InvalidSqlInputError, match="Destination companion"):
            module._reject_companion_collision(adapter, object(), request)
    else:
        module._reject_companion_collision(adapter, object(), request)
    assert looked_up == ["db.events_shard"]


def test_scope_aliases_use_verified_endpoints_and_local_scopes_do_not_equal_remote(monkeypatch):
    setup(monkeypatch)
    monkeypatch.setattr(module, "endpoints", lambda *_a: {("10.0.0.1", 9000)})
    assert module._same_storage_scope(object(), "alias", "physical") is True
    assert module._same_storage_scope(object(), None, "physical") is False
    assert module._same_storage_scope(object(), None, None) is True
    request = options()
    request.ch_creation_policy.distributed_on_cluster = None
    module._reject_facade_collision(object(), request)
