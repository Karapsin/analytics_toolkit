from __future__ import annotations

import builtins
import importlib
import json
import os
import subprocess
import sys
import types
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import analytics_toolkit.general as general_module
import pandas as pd
import pytest
from analytics_toolkit.sql.connection.errors import (
    InvalidSqlInputError,
    SqlConfigError,
    SqlTableReadinessError,
    UnsupportedConnectionTypeError,
)
from analytics_toolkit.sql.ddl.models import CreateSqlTableOptions
from analytics_toolkit.sql.execution.plans import SqlOperationMetadata

config_module = importlib.import_module("analytics_toolkit.sql.connection.config")

config_path_module = importlib.import_module("analytics_toolkit.sql.connection.config_path")

_resolve_calling_base_dir = config_path_module._resolve_calling_base_dir

connections_state_module = importlib.import_module("analytics_toolkit.general.connections")

connection_module = importlib.import_module("analytics_toolkit.sql.connection.get_sql_connection")

api_module = importlib.import_module("analytics_toolkit.sql.dml.transfer.flow.api")

create_sql_table_module = importlib.import_module("analytics_toolkit.sql.ddl.api")

target_replace_module = importlib.import_module("analytics_toolkit.sql.ddl.target_replace")

operation_runner_module = importlib.import_module(
    "analytics_toolkit.sql.execution.operation_runner"
)

transfer_schema_module = importlib.import_module("analytics_toolkit.sql.dml.transfer.schema")

load_sql_table_module = importlib.import_module("analytics_toolkit.sql.dml.load.load_sql_table")

trino_config_module = importlib.import_module("analytics_toolkit.sql.backends.trino.config")

ch_config_module = importlib.import_module("analytics_toolkit.sql.backends.ch.config")

_MISSING = object()


def _fake_drop_target_sqls(*_args: object, **_kwargs: object) -> list[str]:
    return ["drop target;"]


class FakeAirflowConnection:
    def __init__(
        self,
        *,
        conn_type: str | None = None,
        host: str | None = None,
        port: int | None = None,
        login: str | None = None,
        password: str | None = None,
        schema: str | None = None,
        extra_dejson: dict[str, object] | object = _MISSING,
        extra: str | dict[str, object] | None = None,
    ) -> None:
        self.conn_type = conn_type
        self.host = host
        self.port = port
        self.login = login
        self.password = password
        self.schema = schema
        self.extra = extra
        if extra_dejson is not _MISSING:
            self.extra_dejson = extra_dejson


def install_fake_airflow(
    monkeypatch: pytest.MonkeyPatch,
    connections: dict[str, FakeAirflowConnection],
    variables: dict[str, str] | None = None,
) -> None:
    airflow_variables = variables or {}

    class FakeBaseHook:
        @staticmethod
        def get_connection(connection_id: str) -> FakeAirflowConnection:
            try:
                return connections[connection_id]
            except KeyError as exc:
                raise KeyError(connection_id) from exc

    class FakeVariable:
        @staticmethod
        def get(name: str) -> str:
            try:
                return airflow_variables[name]
            except KeyError as exc:
                raise KeyError(name) from exc

    airflow_module = types.ModuleType("airflow")
    hooks_module = types.ModuleType("airflow.hooks")
    base_module = types.ModuleType("airflow.hooks.base")
    models_module = types.ModuleType("airflow.models")
    variable_module = types.ModuleType("airflow.models.variable")
    base_module.BaseHook = FakeBaseHook
    variable_module.Variable = FakeVariable
    models_module.Variable = FakeVariable
    models_module.variable = variable_module
    airflow_module.hooks = hooks_module
    airflow_module.models = models_module
    hooks_module.base = base_module
    monkeypatch.setitem(sys.modules, "airflow", airflow_module)
    monkeypatch.setitem(sys.modules, "airflow.hooks", hooks_module)
    monkeypatch.setitem(sys.modules, "airflow.hooks.base", base_module)
    monkeypatch.setitem(sys.modules, "airflow.models", models_module)
    monkeypatch.setitem(sys.modules, "airflow.models.variable", variable_module)


def _write_cert(relative_path: str, contents: str = "CERT\n") -> Path:
    path = Path.cwd() / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")
    return path


def _install_fake_trino(
    monkeypatch: pytest.MonkeyPatch,
    connect_calls: list[dict[str, object]],
) -> None:
    class FakeBasicAuthentication:
        def __init__(self, user: str, password: str | None) -> None:
            self.user = user
            self.password = password

    fake_auth = types.ModuleType("trino.auth")
    fake_auth.BasicAuthentication = FakeBasicAuthentication
    fake_auth.OAuth2Authentication = lambda: object()
    fake_trino = types.ModuleType("trino")
    fake_trino.auth = fake_auth
    fake_trino.dbapi = types.SimpleNamespace(
        connect=lambda **kwargs: connect_calls.append(kwargs) or object()
    )
    monkeypatch.setitem(sys.modules, "trino", fake_trino)
    monkeypatch.setitem(sys.modules, "trino.auth", fake_auth)


def _direct_trino_config(**overrides: object) -> types.SimpleNamespace:
    values: dict[str, object] = {
        "connection_key": "warehouse",
        "host": "trino.example",
        "port": 8443,
        "user": "analyst",
        "password": "secret",
        "catalog": "iceberg",
        "schema": "analytics",
        "auth_mode": "basic",
        "http_scheme": "https",
        "verify_value": "true",
        "ca_certs": [],
        "request_timeout": 17,
        "source": "coverage",
    }
    values.update(overrides)
    return types.SimpleNamespace(**values)


__all__ = [
    "_MISSING",
    "Callable",
    "CreateSqlTableOptions",
    "FakeAirflowConnection",
    "InvalidSqlInputError",
    "Path",
    "SqlConfigError",
    "SqlOperationMetadata",
    "SqlTableReadinessError",
    "UnsupportedConnectionTypeError",
    "_direct_trino_config",
    "_fake_drop_target_sqls",
    "_install_fake_trino",
    "_resolve_calling_base_dir",
    "_write_cert",
    "api_module",
    "builtins",
    "ch_config_module",
    "config_module",
    "config_path_module",
    "connection_module",
    "connections_state_module",
    "create_sql_table_module",
    "general_module",
    "importlib",
    "install_fake_airflow",
    "json",
    "load_sql_table_module",
    "operation_runner_module",
    "os",
    "pd",
    "pytest",
    "replace",
    "subprocess",
    "sys",
    "target_replace_module",
    "transfer_schema_module",
    "trino_config_module",
    "types",
]
