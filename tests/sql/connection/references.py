from __future__ import annotations

import importlib
import json
import sys
import types
from typing import TYPE_CHECKING, Any

import pytest
from analytics_toolkit.sql.connection.errors import SqlConfigError
from analytics_toolkit.sql.connection.references import (
    resolve_connection_value_references,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

config_module = importlib.import_module("analytics_toolkit.sql.connection.config")


def _install_fake_airflow(
    monkeypatch: pytest.MonkeyPatch,
    *,
    variables: dict[str, Any],
    connections: dict[str, Any] | None = None,
) -> None:
    airflow_connections = connections or {}

    class FakeVariable:
        @staticmethod
        def get(key: str) -> Any:
            return variables[key]

    class FakeBaseHook:
        @staticmethod
        def get_connection(connection_id: str) -> Any:
            return airflow_connections[connection_id]

    airflow_module = types.ModuleType("airflow")
    hooks_module = types.ModuleType("airflow.hooks")
    base_module = types.ModuleType("airflow.hooks.base")
    models_module = types.ModuleType("airflow.models")
    variable_module = types.ModuleType("airflow.models.variable")
    base_module.BaseHook = FakeBaseHook
    variable_module.Variable = FakeVariable
    models_module.Variable = FakeVariable
    models_module.variable = variable_module
    hooks_module.base = base_module
    airflow_module.hooks = hooks_module
    airflow_module.models = models_module
    monkeypatch.setitem(sys.modules, "airflow", airflow_module)
    monkeypatch.setitem(sys.modules, "airflow.hooks", hooks_module)
    monkeypatch.setitem(sys.modules, "airflow.hooks.base", base_module)
    monkeypatch.setitem(sys.modules, "airflow.models", models_module)
    monkeypatch.setitem(sys.modules, "airflow.models.variable", variable_module)


def test_direct_connection_resolves_environment_fields(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    monkeypatch.setenv("GP_HOST_REF", "env-gp.example")
    monkeypatch.setenv("GP_PORT_REF", "15432")
    monkeypatch.setenv("GP_PASSWORD_REF", "env-password")
    write_sql_connections(
        {
            "gp_env": {
                "type": "gp",
                "host": {"from": "env", "key": "GP_HOST_REF"},
                "port": {"from": "env", "key": "GP_PORT_REF"},
                "user": "env-user",
                "password": {"from": "env", "key": "GP_PASSWORD_REF"},
                "database": "analytics",
            }
        }
    )

    config = config_module.get_connection_config("gp_env")

    assert config.host == "env-gp.example"
    assert config.port == 15432
    assert config.password == "env-password"
    assert "env-password" not in repr(config)


def test_environment_json_path_can_supply_nested_and_root_values(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    monkeypatch.setenv(
        "CH_RUNTIME_CONFIG",
        json.dumps(
            {
                "credentials": {"password": "json-password"},
                "settings": {"connect_timeout": "500", "max_threads": 4},
            }
        ),
    )
    write_sql_connections(
        {
            "ch_env": {
                "type": "ch",
                "host": "ch.example",
                "user": "ch-user",
                "password": {
                    "from": "env",
                    "key": "CH_RUNTIME_CONFIG",
                    "path": ["credentials", "password"],
                },
                "settings": {
                    "from": "env",
                    "key": "CH_RUNTIME_CONFIG",
                    "path": ["settings"],
                },
            }
        }
    )

    config = config_module.get_connection_config("ch_env")

    assert config.password == "json-password"
    assert config.settings == {"connect_timeout": "500", "max_threads": 4}

    monkeypatch.setenv("CH_SETTINGS", json.dumps({"max_threads": 2}))
    resolved = resolve_connection_value_references(
        "ch_env",
        {
            "settings": {
                "from": "env",
                "key": "CH_SETTINGS",
                "path": [],
            }
        },
    )
    assert resolved["settings"] == {"max_threads": 2}


def test_airflow_variable_resolves_scalar_and_json_paths(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    _install_fake_airflow(
        monkeypatch,
        variables={
            "TRINO_PASSWORD_AF": "airflow-password",
            "S3_AF": {
                "credentials": {
                    "access_key": "airflow-access",
                    "secret_key": "airflow-secret",
                }
            },
        },
    )
    write_sql_connections(
        {
            "trino_af": {
                "type": "trino",
                "host": "trino.example",
                "user": "trino-user",
                "password": {
                    "from": "airflow_variable",
                    "key": "TRINO_PASSWORD_AF",
                },
                "aws_access_key_id": {
                    "from": "airflow_variable",
                    "key": "S3_AF",
                    "path": ["credentials", "access_key"],
                },
                "aws_secret_access_key": {
                    "from": "airflow_variable",
                    "key": "S3_AF",
                    "path": ["credentials", "secret_key"],
                },
            }
        }
    )

    config = config_module.get_connection_config("trino_af")

    assert config.password == "airflow-password"
    assert config.access_key_id == "airflow-access"
    assert config.secret_access_key == "airflow-secret"
    assert "airflow-password" not in repr(config)
    assert "airflow-secret" not in repr(config)


def test_airflow_source_allows_external_s3_references(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, Any]], Path],
) -> None:
    monkeypatch.setenv("TRINO_REQUEST_TIMEOUT", "900")
    connection = types.SimpleNamespace(
        conn_type="trino",
        host="trino.example",
        port=8443,
        login="trino-user",
        password="trino-password",
        schema=None,
        extra_dejson={"catalog": "iceberg", "schema": "sandbox"},
        extra=None,
    )
    _install_fake_airflow(
        monkeypatch,
        variables={
            "S3_ACCESS_AF": "airflow-access",
            "S3_SECRET_AF": "airflow-secret",
        },
        connections={"AirTrino": connection},
    )
    write_sql_connections(
        {
            "source": "airflow",
            "connections": {
                "trino": {
                    "type": "trino",
                    "connection_id": "AirTrino",
                    "request_timeout": {
                        "from": "env",
                        "key": "TRINO_REQUEST_TIMEOUT",
                    },
                    "aws_access_key_id": {
                        "from": "airflow_variable",
                        "key": "S3_ACCESS_AF",
                    },
                    "aws_secret_access_key": {
                        "from": "airflow_variable",
                        "key": "S3_SECRET_AF",
                    },
                }
            },
        }
    )

    config = config_module.get_connection_config("trino")

    assert config.request_timeout == 900
    assert config.access_key_id == "airflow-access"
    assert config.secret_access_key == "airflow-secret"


@pytest.mark.parametrize(
    ("reference", "message"),
    [
        (
            {"from": 1, "key": "VALUE"},
            "must be '.secrets', 'env', or 'airflow_variable'",
        ),
        (
            {"from": "vault", "key": "VALUE"},
            "must be '.secrets', 'env', or 'airflow_variable'",
        ),
        (
            {"from": ".secrets", "key": "invalid-key"},
            "must use a shell identifier",
        ),
        ({"from": "env", "key": ""}, "must be a non-empty string"),
        ({"from": "env", "key": "VALUE", "fallback": "x"}, "unsupported field"),
        ({"from": "env", "key": "VALUE", "path": "x"}, "array of non-empty strings"),
        ({"from": "env", "key": "VALUE", "path": [""]}, "array of non-empty strings"),
    ],
)
def test_reference_descriptor_validation(
    monkeypatch: pytest.MonkeyPatch,
    reference: dict[str, object],
    message: str,
) -> None:
    monkeypatch.setenv("VALUE", "value")

    with pytest.raises(SqlConfigError, match=message):
        resolve_connection_value_references("db", {"password": reference})


def test_reference_failures_do_not_expose_source_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BROKEN_JSON", "super-secret-not-json")
    with pytest.raises(SqlConfigError, match="must contain valid JSON") as caught:
        resolve_connection_value_references(
            "db",
            {
                "password": {
                    "from": "env",
                    "key": "BROKEN_JSON",
                    "path": ["password"],
                }
            },
        )
    assert "super-secret-not-json" not in str(caught.value)

    monkeypatch.setenv("VALID_JSON", json.dumps({"password": "super-secret"}))
    with pytest.raises(SqlConfigError, match="was not found") as caught:
        resolve_connection_value_references(
            "db",
            {
                "password": {
                    "from": "env",
                    "key": "VALID_JSON",
                    "path": ["missing"],
                }
            },
        )
    assert "super-secret" not in str(caught.value)


def test_missing_environment_and_airflow_support_are_clear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MISSING_CONNECTION_VALUE", raising=False)
    with pytest.raises(SqlConfigError, match="is not set"):
        resolve_connection_value_references(
            "db",
            {
                "password": {
                    "from": "env",
                    "key": "MISSING_CONNECTION_VALUE",
                }
            },
        )

    monkeypatch.setitem(sys.modules, "airflow.models.variable", None)
    with pytest.raises(SqlConfigError, match="support is unavailable"):
        resolve_connection_value_references(
            "db",
            {
                "password": {
                    "from": "airflow_variable",
                    "key": "MISSING_AF",
                }
            },
        )

    _install_fake_airflow(monkeypatch, variables={})
    with pytest.raises(SqlConfigError, match="Could not resolve Airflow Variable"):
        resolve_connection_value_references(
            "db",
            {
                "password": {
                    "from": "airflow_variable",
                    "key": "MISSING_AF",
                }
            },
        )


def test_routing_fields_stay_literal_and_literal_mappings_are_preserved() -> None:
    with pytest.raises(SqlConfigError, match=r"routing field 'type'.*literal"):
        resolve_connection_value_references(
            "db",
            {"type": {"from": "env", "key": "DB_TYPE"}},
        )

    settings = {"from": "literal-setting", "max_threads": 4}
    resolved = resolve_connection_value_references("db", {"settings": settings})
    assert resolved["settings"] == settings

    with pytest.raises(SqlConfigError, match="resolver field 'from' must be 'extra'"):
        config_module._resolve_airflow_extra_resolver(
            "request_timeout",
            {"from": "env", "key": "REQUEST_TIMEOUT"},
            {},
            "AirTrino",
        )
    with pytest.raises(SqlConfigError, match="field 'host' must be a string"):
        config_module._optional_string({"host": 1}, "db", "host")
