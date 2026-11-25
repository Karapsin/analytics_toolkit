"""Keep legacy row-batch DBAPI fakes at the actual driver boundary."""

from __future__ import annotations

import importlib

import pytest

from tests.sql._support.row_batches import RecordingSourceCursor


@pytest.fixture(autouse=True)
def isolated_recording_source_driver(monkeypatch):
    psycopg2 = importlib.import_module("psycopg2")
    extensions = importlib.import_module("psycopg2.extensions")

    register = extensions.register_type

    def register_type(caster, scope=None):
        if not isinstance(scope, RecordingSourceCursor):
            return register(caster, scope)
        registry = scope.__dict__.setdefault("registered_types", {})
        for oid in caster.values:
            registry[oid] = caster
        return None

    def unexpected_connection(*_args, **_kwargs):
        message = "Row-batch tests must supply a fake Greenplum connection."
        raise AssertionError(message)

    monkeypatch.setattr(extensions, "register_type", register_type)
    monkeypatch.setattr(psycopg2, "connect", unexpected_connection)
