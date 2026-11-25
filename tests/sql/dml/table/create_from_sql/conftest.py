"""Query-creation fixtures must explicitly own every driver connection."""

from __future__ import annotations

import pytest
from analytics_toolkit.sql.backends import get_backend_adapter
from psycopg2 import extensions

from tests.sql._support.create_from_sql import FakeDbapiCursor


class UnexpectedQueryDriverError(AssertionError):
    analytics_toolkit_sql_retry_safe = False


@pytest.fixture(autouse=True)
def _owned_query_driver_boundaries(monkeypatch):
    def open_unowned(*_args, **_kwargs):
        message = "Query-creation test requested an unowned driver connection."
        raise UnexpectedQueryDriverError(message)

    def register(caster, cursor=None):
        if not isinstance(cursor, FakeDbapiCursor):
            message = "Query-creation test must own its fake cursor codec registration."
            raise UnexpectedQueryDriverError(message)
        if not hasattr(cursor, "casters"):
            cursor.casters = {}
        for oid in caster.values:
            cursor.casters[oid] = caster

    for backend in ("gp", "trino", "ch"):
        monkeypatch.setattr(get_backend_adapter(backend), "open_connection", open_unowned)
    monkeypatch.setattr(extensions, "register_type", register)
