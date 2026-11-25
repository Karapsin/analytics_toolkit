"""Keep transfer protocol tests on explicitly owned fake driver surfaces."""

from __future__ import annotations

import importlib

import psycopg2
from psycopg2 import extensions


def install_fake_gp_cursor_boundary(monkeypatch, cursor_types):
    def register(caster, scope=None):
        assert isinstance(scope, cursor_types), type(scope)
        if not hasattr(scope, "casters"):
            scope.casters = {}
        for oid in caster.values:
            scope.casters[oid] = caster

    def unexpected_connect(*_args, **_kwargs):
        message = "Transfer protocol test escaped its owned fake driver factory."
        raise AssertionError(message)

    monkeypatch.setattr(extensions, "register_type", register)
    monkeypatch.setattr(psycopg2, "connect", unexpected_connect)


def install_transfer_factories(monkeypatch, connect):
    for name in (
        "flow.attempt",
        "flow.adaptive_input",
        "flow.finalize",
        "runtime.retry",
    ):
        module = importlib.import_module(f"analytics_toolkit.sql.dml.transfer.{name}")
        monkeypatch.setattr(module, "get_sql_connection", connect)
