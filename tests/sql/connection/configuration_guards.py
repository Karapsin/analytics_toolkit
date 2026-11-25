from __future__ import annotations

from dataclasses import replace
from functools import partial
from types import SimpleNamespace

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend
from analytics_toolkit.sql.connection.configuration_guards import check_connection_configuration
from analytics_toolkit.sql.execution.backend_options import (
    backend_operation,
    current_backend_operation,
)

from tests.sql._support.connection_config import config_module, connection_module


def test_unguarded_configuration_check_has_no_operation_or_side_effect():
    assert current_backend_operation() is None
    check_connection_configuration(SimpleNamespace(connection_key="unbound"))
    assert current_backend_operation() is None


@pytest.mark.parametrize("host_factory", [False, True])
def test_factory_guards_receive_the_actual_parsed_configuration_before_open(
    host_factory, monkeypatch
):
    alias = "ch" if host_factory else "gp"
    expected = config_module.get_connection_config(alias)
    calls = []
    backend = get_backend(alias)
    opened = []
    monkeypatch.setattr(connection_module, "get_connection_config", lambda _alias: expected)
    monkeypatch.setattr(
        type(backend),
        "open_connection",
        lambda _self, config, **_kwargs: opened.append(config) or object(),
    )

    def guard(actual_alias, actual_config):
        assert actual_alias == expected.connection_key
        assert actual_config is expected
        assert not opened
        calls.append(actual_alias)

    with backend_operation() as operation:
        operation.resources["connection_configuration_guards"] = {"frozen": guard}
        if host_factory:
            connection_module.get_ch_connection_for_host("ch", "replica-host")
        else:
            sql.get_sql_connection("gp")
    assert calls == [alias]
    assert len(opened) == 1
    assert opened[0].host == ("replica-host" if host_factory else expected.host)


@pytest.mark.parametrize("host_factory", [False, True])
@pytest.mark.parametrize("field", ["host", "database", "password"])
def test_configuration_mismatch_prevents_reconnect_and_cleanup_from_opening_new_storage(
    host_factory, field, monkeypatch
):
    alias = "ch" if host_factory else "gp"
    frozen = config_module.get_connection_config(alias)
    changed = replace(frozen, **{field: "changed-private-value"})
    monkeypatch.setattr(connection_module, "get_connection_config", lambda _alias: changed)
    backend = get_backend(alias)
    opened = []
    monkeypatch.setattr(
        type(backend), "open_connection", lambda *_args, **_kwargs: opened.append(True)
    )
    message = "Frozen connection configuration changed."

    def guard(_alias, actual_config):
        if actual_config != frozen:
            raise ValueError(message)

    call = (
        partial(connection_module.get_ch_connection_for_host, alias, "replica-host")
        if host_factory
        else partial(sql.get_sql_connection, alias)
    )
    with backend_operation() as operation:
        operation.resources["connection_configuration_guards"] = {"frozen": guard}
        for _phase in ["reconnect", "cleanup"]:
            with pytest.raises(ValueError, match=message):
                call()
    assert not opened
    assert current_backend_operation() is None


def test_guard_callbacks_can_unregister_without_skipping_other_frozen_inputs():
    calls = []
    with backend_operation() as operation:

        def unregister(alias, _config):
            calls.append(("first", alias))
            operation.resources["connection_configuration_guards"].clear()

        operation.resources["connection_configuration_guards"] = {
            "first": unregister,
            "second": lambda alias, _config: calls.append(("second", alias)),
        }
        check_connection_configuration(SimpleNamespace(connection_key="source"))
        assert calls == [("first", "source"), ("second", "source")]
        assert not operation.resources["connection_configuration_guards"]


def test_an_unguarded_active_operation_keeps_ordinary_connection_semantics():
    with backend_operation() as operation:
        check_connection_configuration(SimpleNamespace(connection_key="ordinary"))
        assert current_backend_operation() is operation
        assert not operation.resources
