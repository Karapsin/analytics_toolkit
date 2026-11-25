"""Real GP typed checkpoints for scalar text-stage numeric transfer fixtures."""

from __future__ import annotations

from copy import deepcopy

import psycopg2
from analytics_toolkit.sql.dml.transfer.flow import (
    adaptive_input,
    attempt,
    keyed_pipeline,
    native_assignment,
)
from sqlglot import exp

from tests.sql._support.gp_numeric_text import NumericDatabase
from tests.sql._support.transfer_native_assignment import (
    NativeTransferConnection,
    NativeTransferDatabase,
    install_native_transfer,
)


def install_numeric_text_transfer(monkeypatch, *, rows, final_type, stage_type="TEXT"):
    """Return a stateful GP database; callers invoke the public transfer facade.

    Source alias/type is Trino VARCHAR, destination is GP sandbox.events, and
    the explicit stage has one `id` column. Only native server I/O is fake;
    adapters, parameter adaptation, scoped CREATE, certification and SQL CASTs
    execute normally. This server supports NUMERIC NaN but not NUMERIC infinity.
    """
    original_assignment = NativeTransferDatabase.assign

    def assign(value, native, *, explicit=False):
        numeric = native.upper().replace(" ", "")
        if numeric.startswith(("NUMERIC", "DECIMAL")) or numeric in {
            "REAL",
            "FLOAT4",
            "DOUBLEPRECISION",
            "FLOAT8",
        }:
            if value is None:
                return None
            return NumericDatabase.decode_value(exp.Literal.string(str(value)), native)
        return original_assignment(value, native, explicit=explicit)

    monkeypatch.setattr(NativeTransferDatabase, "assign", staticmethod(assign))
    database = install_native_transfer(
        monkeypatch,
        rows=deepcopy(rows),
        target_schema={"id": stage_type},
        source_types={"id": "VARCHAR"},
        source_key="trino",
        source_backend="trino",
    )
    original_close = NativeTransferConnection.close

    def close(connection):
        connection.close_count = getattr(connection, "close_count", 0) + 1
        return original_close(connection)

    monkeypatch.setattr(NativeTransferConnection, "close", close)
    original_fresh_connection = native_assignment.run_with_fresh_connection
    fake_open = attempt.get_sql_connection

    def run_with_fake_connection(key, role, operation, **kwargs):
        # Exercise the real fresh-handle lifecycle and guarded cleanup, replacing
        # only its definition-time network factory with this installed database.
        kwargs.setdefault("open_connection", fake_open)
        return original_fresh_connection(key, role, operation, **kwargs)

    monkeypatch.setattr(native_assignment, "run_with_fresh_connection", run_with_fake_connection)
    database.target.schema = {"id": final_type}
    sentinel = assign("99.123456789", final_type)
    database.target.rows = [{"id": sentinel}, {"id": None}]
    database.sentinel_rows = deepcopy(database.target.rows)
    database.original_source_rows = deepcopy(rows)
    database.source_completions = []
    original_read = attempt.iter_source_batches

    def read(*args, **kwargs):
        yield from original_read(*args, **kwargs)
        database.source_completions.append(len(database.commands))

    for module in (attempt, adaptive_input, keyed_pipeline):
        monkeypatch.setattr(module, "iter_source_batches", read)

    def unexpected_connect(*_args, **_kwargs):
        message = "Numeric text transfer escaped its owned fake connection boundary."
        raise AssertionError(message)

    monkeypatch.setattr(psycopg2, "connect", unexpected_connect)
    return database
