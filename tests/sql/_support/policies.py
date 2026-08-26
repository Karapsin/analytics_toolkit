from __future__ import annotations

import importlib
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.creation_policy import (
    ClickHouseCreationPolicy,
    resolve_clickhouse_creation_policy,
)
from analytics_toolkit.sql.connection.ddl_defaults import legacy_clickhouse_scope
from analytics_toolkit.sql.connection.errors import SqlConfigError
from analytics_toolkit.sql.dml.transfer.runtime.models import RowBatch

config_module = importlib.import_module("analytics_toolkit.sql.connection.config")

ddl_options_module = importlib.import_module("analytics_toolkit.sql.dml.ddl_options")

parquet_stage_module = importlib.import_module(
    "analytics_toolkit.sql.dml.transfer.flow.parquet_stage"
)

storage_module = importlib.import_module("analytics_toolkit.sql.backends.trino.storage")

wait_module = importlib.import_module("analytics_toolkit.sql.backends.ch.wait")

creation_policy_module = importlib.import_module(
    "analytics_toolkit.sql.backends.ch.creation_policy"
)

reconfigure_wait_module = importlib.import_module(
    "analytics_toolkit.sql.backends.ch.reconfigure_wait"
)

transfer_options_module = importlib.import_module("analytics_toolkit.sql.dml.transfer.flow.options")

transfer_finalize_module = importlib.import_module(
    "analytics_toolkit.sql.dml.transfer.flow.finalize"
)

trino_adapter_module = importlib.import_module("analytics_toolkit.sql.backends.trino.adapter")


def _trino_config(**overrides: object) -> dict[str, object]:
    return {
        "type": "trino",
        "host": "trino.example",
        "user": "user",
        **overrides,
    }


def _ch_config(**overrides: object) -> dict[str, object]:
    return {
        "type": "ch",
        "host": "ch.example",
        "user": "user",
        "password": "password",
        **overrides,
    }


__all__ = [
    "TYPE_CHECKING",
    "ClickHouseCreationPolicy",
    "RowBatch",
    "SimpleNamespace",
    "SqlConfigError",
    "_ch_config",
    "_trino_config",
    "config_module",
    "creation_policy_module",
    "ddl_options_module",
    "importlib",
    "json",
    "legacy_clickhouse_scope",
    "parquet_stage_module",
    "pd",
    "pytest",
    "reconfigure_wait_module",
    "resolve_clickhouse_creation_policy",
    "sql",
    "storage_module",
    "transfer_finalize_module",
    "transfer_options_module",
    "trino_adapter_module",
    "wait_module",
]
