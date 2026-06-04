from __future__ import annotations

import builtins
import importlib
import sys
import types
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

config_module = importlib.import_module("analytics_toolkit.sql.connection.config")
connection_module = importlib.import_module(
    "analytics_toolkit.sql.connection.get_sql_connection"
)
api_module = importlib.import_module("analytics_toolkit.sql.dml.transfer.flow.api")
create_sql_table_module = importlib.import_module(
    "analytics_toolkit.sql.ddl.create_sql_table"
)
load_sql_table_module = importlib.import_module(
    "analytics_toolkit.sql.dml.load.load_sql_table"
)

_MISSING = object()


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


def test_connection_alias_resolves_backend() -> None:
    config = config_module.get_connection_config("gp_sandbox")

    assert config.connection_key == "gp_sandbox"
    assert config.backend == "gp"
    assert config.database == "sandbox"
    assert config.connect_timeout == 30
    assert config.keepalives is True
    assert config.keepalives_idle == 60
    assert config.keepalives_interval == 10
    assert config.keepalives_count == 3


def test_gp_connection_uses_liveness_defaults(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    connect_calls: list[dict[str, object]] = []
    fake_psycopg2 = types.SimpleNamespace(
        connect=lambda **kwargs: connect_calls.append(kwargs) or object()
    )
    monkeypatch.setitem(sys.modules, "psycopg2", fake_psycopg2)

    connection_module.get_sql_connection("gp")

    output = capsys.readouterr().out
    assert "[get_sql_connection] [gp/gp] [connect] Opening connection" in output
    assert connect_calls == [
        {
            "host": "gp.example",
            "port": 5432,
            "user": "user",
            "password": "password",
            "dbname": "db",
            "connect_timeout": 30,
            "keepalives": 1,
            "keepalives_idle": 60,
            "keepalives_interval": 10,
            "keepalives_count": 3,
        }
    ]


def test_gp_connection_liveness_options_can_be_overridden(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections(
        {
            "gp_custom": {
                "type": "gp",
                "host": "gp.example",
                "port": 5432,
                "user": "user",
                "password": "password",
                "database": "db",
                "connect_timeout": "7",
                "keepalives": "false",
                "keepalives_idle": "8",
                "keepalives_interval": 9,
                "keepalives_count": 4,
            }
        }
    )
    connect_calls: list[dict[str, object]] = []
    fake_psycopg2 = types.SimpleNamespace(
        connect=lambda **kwargs: connect_calls.append(kwargs) or object()
    )
    monkeypatch.setitem(sys.modules, "psycopg2", fake_psycopg2)

    config = config_module.get_connection_config("gp_custom")
    connection_module.get_sql_connection("gp_custom")

    assert config.connect_timeout == 7
    assert config.keepalives is False
    assert config.keepalives_idle == 8
    assert config.keepalives_interval == 9
    assert config.keepalives_count == 4
    assert connect_calls == [
        {
            "host": "gp.example",
            "port": 5432,
            "user": "user",
            "password": "password",
            "dbname": "db",
            "connect_timeout": 7,
            "keepalives": 0,
        }
    ]


def test_clickhouse_connection_disables_auto_session_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = object()
    setting_calls: list[tuple[str, object]] = []
    client_calls: list[dict[str, object]] = []

    fake_clickhouse_connect = types.SimpleNamespace(
        common=types.SimpleNamespace(
            set_setting=lambda name, value: setting_calls.append((name, value))
        ),
        get_client=lambda **kwargs: client_calls.append(kwargs) or client,
    )
    monkeypatch.setitem(sys.modules, "clickhouse_connect", fake_clickhouse_connect)
    monkeypatch.setitem(
        sys.modules,
        "clickhouse_connect.common",
        fake_clickhouse_connect.common,
    )

    result = connection_module.get_sql_connection("ch")

    assert result is client
    assert setting_calls == [("autogenerate_session_id", False)]
    assert client_calls == [
        {
            "host": "ch.example",
            "port": 8123,
            "username": "user",
            "password": "password",
            "secure": False,
            "database": "default",
        }
    ]


def test_unknown_connection_key_raises_config_error() -> None:
    with pytest.raises(config_module.UnsupportedConnectionTypeError):
        config_module.get_connection_config("missing")


def test_malformed_connections_file_raises_config_error(tmp_path: Path) -> None:
    (tmp_path / ".connections").write_text("{not json", encoding="utf-8")

    with pytest.raises(config_module.SqlConfigError):
        config_module.get_connection_config("gp")


def test_missing_connections_file_ignores_legacy_backend_env_vars(
    monkeypatch,
    tmp_path: Path,
) -> None:
    (tmp_path / ".connections").unlink()
    monkeypatch.setenv("GP_HOST", "legacy-host")
    monkeypatch.setenv("GP_USER", "legacy-user")
    monkeypatch.setenv("GP_PASSWORD", "legacy-password")
    monkeypatch.setenv("GP_DATABASE", "legacy-db")
    monkeypatch.setenv(
        "SQL_CONNECTIONS",
        (
            '{"gp":{"type":"gp","host":"legacy","user":"legacy",'
            '"password":"legacy","database":"legacy"}}'
        ),
    )

    with pytest.raises(config_module.SqlConfigError, match=".connections"):
        config_module.get_connection_config("gp")


def test_airflow_gp_connection_maps_airflow_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_airflow(
        monkeypatch,
        {
            "AirGp": FakeAirflowConnection(
                conn_type="postgres",
                host="air-gp.example",
                port=15432,
                login="air-user",
                password="air-password",
                schema="air_db",
                extra_dejson={
                    "connect_timeout": "9",
                    "keepalives": "false",
                    "keepalives_idle": "20",
                    "keepalives_interval": 5,
                    "keepalives_count": "2",
                },
            )
        },
    )

    config = config_module.airflow_connection_config("AirGp")

    assert isinstance(config, config_module.GpConfig)
    assert config.connection_key == "AirGp"
    assert config.backend == "gp"
    assert config.host == "air-gp.example"
    assert config.port == 15432
    assert config.user == "air-user"
    assert config.password == "air-password"
    assert config.database == "air_db"
    assert config.connect_timeout == 9
    assert config.keepalives is False
    assert config.keepalives_idle == 20
    assert config.keepalives_interval == 5
    assert config.keepalives_count == 2


def test_airflow_trino_connection_maps_extras(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_airflow(
        monkeypatch,
        {
            "AirTrino": FakeAirflowConnection(
                conn_type="trino",
                host="air-trino.example",
                port=8443,
                login="trino-user",
                password="trino-password",
                extra_dejson={
                    "catalog": "iceberg",
                    "schema": "sandbox",
                    "auth_mode": "basic",
                    "http_scheme": "https",
                    "verify": False,
                    "insert_chunk_size": "500",
                    "request_timeout": "700",
                    "source": "airflow",
                },
            )
        },
    )

    config = config_module.airflow_connection_config("AirTrino")

    assert isinstance(config, config_module.TrinoConfig)
    assert config.connection_key == "AirTrino"
    assert config.backend == "trino"
    assert config.host == "air-trino.example"
    assert config.port == 8443
    assert config.user == "trino-user"
    assert config.password == "trino-password"
    assert config.catalog == "iceberg"
    assert config.schema == "sandbox"
    assert config.auth_mode == "basic"
    assert config.http_scheme == "https"
    assert config.verify_value == "false"
    assert config.insert_chunk_size == 500
    assert config.request_timeout == 700
    assert config.source == "airflow"


def test_airflow_clickhouse_connection_maps_airflow_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_airflow(
        monkeypatch,
        {
            "AirCh": FakeAirflowConnection(
                conn_type="clickhouse",
                host="air-ch.example",
                port=9440,
                login="ch-user",
                password="ch-password",
                schema="default",
                extra_dejson={
                    "secure": "true",
                    "verify": False,
                    "ca_certs_variable": "clickhouse_ca_cert",
                    "connect_timeout": "11",
                    "send_receive_timeout": "6001",
                    "settings": {"use_numpy": True},
                    "interface": "https",
                    "query_limit": "100",
                    "query_retries": "4",
                    "client_name": "analytics-toolkit",
                },
            )
        },
    )

    config = config_module.airflow_connection_config("AirCh")

    assert isinstance(config, config_module.ChConfig)
    assert config.connection_key == "AirCh"
    assert config.backend == "ch"
    assert config.host == "air-ch.example"
    assert config.port == 9440
    assert config.user == "ch-user"
    assert config.password == "ch-password"
    assert config.database == "default"
    assert config.secure is True
    assert config.verify_value == "false"
    assert config.ca_cert is None
    assert config.ca_cert_variable == "clickhouse_ca_cert"
    assert config.connect_timeout == 11
    assert config.send_receive_timeout == 6001
    assert config.settings == {"use_numpy": True}
    assert config.interface == "https"
    assert config.query_limit == 100
    assert config.query_retries == 4
    assert config.client_name == "analytics-toolkit"


def test_airflow_clickhouse_connection_uses_dag_compatible_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_airflow(
        monkeypatch,
        {
            "AirCh": FakeAirflowConnection(
                conn_type="clickhouse",
                host="air-ch.example",
                login="ch-user",
                password="ch-password",
                schema="default",
            )
        },
    )

    config = config_module.airflow_connection_config("AirCh")

    assert isinstance(config, config_module.ChConfig)
    assert config.send_receive_timeout == 6000
    assert config.settings == {"connect_timeout": "500"}


def test_airflow_clickhouse_file_overrides_airflow_extras(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, object]], Path],
) -> None:
    write_sql_connections(
        {
            "source": "airflow",
            "connections": {
                "AirCh": {
                    "type": "ch",
                    "send_receive_timeout": 12,
                    "settings": {"max_threads": 4},
                    "ca_cert": "/local/ca.pem",
                }
            },
        }
    )
    install_fake_airflow(
        monkeypatch,
        {
            "AirCh": FakeAirflowConnection(
                conn_type="clickhouse",
                host="air-ch.example",
                login="ch-user",
                password="ch-password",
                schema="default",
                extra_dejson={
                    "send_receive_timeout": 6000,
                    "settings": {"connect_timeout": "500"},
                    "ca_certs_variable": "clickhouse_ca_cert",
                },
            )
        },
    )

    config = config_module.get_connection_config("AirCh")

    assert isinstance(config, config_module.ChConfig)
    assert config.send_receive_timeout == 12
    assert config.settings == {"max_threads": 4}
    assert config.ca_cert == "/local/ca.pem"
    assert config.ca_cert_variable is None


def test_clickhouse_connection_passes_optional_connector_settings(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections(
        {
            "ch_custom": {
                "type": "ch",
                "host": "ch.example",
                "port": 8123,
                "user": "user",
                "password": "password",
                "database": "default",
                "secure": True,
                "verify": "false",
                "ca_certs_variable": "clickhouse_ca_cert",
                "connect_timeout": "11",
                "send_receive_timeout": "6001",
                "settings": {"connect_timeout": "500", "use_numpy": True},
                "interface": "https",
                "query_limit": "100",
                "query_retries": "4",
                "client_name": "analytics-toolkit",
            }
        }
    )
    install_fake_airflow(
        monkeypatch,
        {},
        variables={"clickhouse_ca_cert": "/airflow/ca.pem"},
    )
    client = object()
    client_calls: list[dict[str, object]] = []
    fake_clickhouse_connect = types.SimpleNamespace(
        common=types.SimpleNamespace(set_setting=lambda name, value: None),
        get_client=lambda **kwargs: client_calls.append(kwargs) or client,
    )
    monkeypatch.setitem(sys.modules, "clickhouse_connect", fake_clickhouse_connect)
    monkeypatch.setitem(
        sys.modules,
        "clickhouse_connect.common",
        fake_clickhouse_connect.common,
    )

    result = connection_module.get_sql_connection("ch_custom")

    assert result is client
    assert client_calls == [
        {
            "host": "ch.example",
            "port": 8123,
            "username": "user",
            "password": "password",
            "secure": True,
            "database": "default",
            "verify": False,
            "ca_cert": "/airflow/ca.pem",
            "connect_timeout": 11,
            "send_receive_timeout": 6001,
            "settings": {"connect_timeout": "500", "use_numpy": True},
            "interface": "https",
            "query_limit": 100,
            "query_retries": 4,
            "client_name": "analytics-toolkit",
        }
    ]


def test_clickhouse_settings_must_be_mapping(
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections(
        {
            "ch_bad": {
                "type": "ch",
                "host": "ch.example",
                "user": "user",
                "password": "password",
                "settings": ["use_numpy"],
            }
        }
    )

    with pytest.raises(config_module.SqlConfigError, match="settings"):
        config_module.get_connection_config("ch_bad")


def test_clickhouse_missing_ca_cert_variable_is_clear_config_error(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections(
        {
            "ch_custom": {
                "type": "ch",
                "host": "ch.example",
                "user": "user",
                "password": "password",
                "ca_certs_variable": "missing_certificate",
            }
        }
    )
    install_fake_airflow(monkeypatch, {}, variables={})
    fake_clickhouse_connect = types.SimpleNamespace(
        common=types.SimpleNamespace(set_setting=lambda name, value: None),
        get_client=lambda **kwargs: object(),
    )
    monkeypatch.setitem(sys.modules, "clickhouse_connect", fake_clickhouse_connect)
    monkeypatch.setitem(
        sys.modules,
        "clickhouse_connect.common",
        fake_clickhouse_connect.common,
    )

    with pytest.raises(
        config_module.SqlConfigError,
        match="Could not resolve Airflow Variable 'missing_certificate'",
    ):
        connection_module.get_sql_connection("ch_custom")


def test_airflow_connections_are_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_airflow(
        monkeypatch,
        {
            "gp": FakeAirflowConnection(
                conn_type="postgres",
                host="air-gp.example",
                login="air-user",
                password="air-password",
                schema="air_db",
            )
        },
    )

    default_config = config_module.get_connection_config("gp")
    with config_module.use_airflow_connections():
        airflow_config = config_module.get_connection_config("gp")
    restored_config = config_module.get_connection_config("gp")

    assert default_config.host == "gp.example"
    assert isinstance(airflow_config, config_module.GpConfig)
    assert airflow_config.host == "air-gp.example"
    assert restored_config.host == "gp.example"


def test_airflow_context_resolves_public_sql_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_airflow(
        monkeypatch,
        {
            "AirGp": FakeAirflowConnection(
                host="air-gp.example",
                port=15432,
                login="air-user",
                password="air-password",
                schema="air_db",
            )
        },
    )
    connect_calls: list[dict[str, object]] = []
    fake_psycopg2 = types.SimpleNamespace(
        connect=lambda **kwargs: connect_calls.append(kwargs) or object()
    )
    monkeypatch.setitem(sys.modules, "psycopg2", fake_psycopg2)

    with config_module.use_airflow_connections({"AirGp": "gp"}):
        connection_module.get_sql_connection("AirGp")

    assert connect_calls == [
        {
            "host": "air-gp.example",
            "port": 15432,
            "user": "air-user",
            "password": "air-password",
            "dbname": "air_db",
            "connect_timeout": 30,
            "keepalives": 1,
            "keepalives_idle": 60,
            "keepalives_interval": 10,
            "keepalives_count": 3,
        }
    ]


def test_transfer_options_use_airflow_context_alias_backends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_airflow(
        monkeypatch,
        {
            "AirTrino": FakeAirflowConnection(
                conn_type="trino",
                host="air-trino.example",
                login="trino-user",
                extra_dejson={"catalog": "iceberg", "schema": "sandbox"},
            ),
            "AirGp": FakeAirflowConnection(
                conn_type="postgres",
                host="air-gp.example",
                login="air-user",
                password="air-password",
                schema="air_db",
            ),
        },
    )

    with config_module.use_airflow_connections():
        options = api_module.build_transfer_options(
            from_db="AirTrino",
            to_db="AirGp",
            from_sql="select 1",
            to_table="schema.target",
            gp_distributed_by_key=["id"],
        )

    assert options.from_db_key == "AirTrino"
    assert options.from_db_backend == "trino"
    assert options.to_db_key == "AirGp"
    assert options.to_db_backend == "gp"
    assert options.gp_distributed_by_key == ["id"]


def test_unknown_airflow_connection_id_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_airflow(monkeypatch, {})

    with pytest.raises(
        config_module.UnsupportedConnectionTypeError,
        match="Unknown Airflow connection ID: missing",
    ):
        config_module.airflow_connection_config("missing", "gp")


def test_airflow_connection_config_requires_airflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for module_name in ("airflow.hooks.base", "airflow.hooks", "airflow"):
        monkeypatch.delitem(sys.modules, module_name, raising=False)

    real_import = builtins.__import__

    def fake_import(
        name: str,
        globals: object | None = None,
        locals: object | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        if name.startswith("airflow"):
            raise ImportError("airflow is unavailable")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(config_module.SqlConfigError, match="apache-airflow"):
        config_module.airflow_connection_config("missing", "gp")


def test_airflow_connections_file_resolves_alias_without_context(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, object]], Path],
) -> None:
    write_sql_connections(
        {
            "source": "airflow",
            "connections": {
                "AirGp": {
                    "type": "gp",
                    "connect_timeout": "8",
                }
            },
        }
    )
    install_fake_airflow(
        monkeypatch,
        {
            "AirGp": FakeAirflowConnection(
                conn_type="postgres",
                host="air-gp.example",
                port=15432,
                login="air-user",
                password="air-password",
                schema="air_db",
            )
        },
    )

    config = config_module.get_connection_config("AirGp")

    assert isinstance(config, config_module.GpConfig)
    assert config.connection_key == "airgp"
    assert config.backend == "gp"
    assert config.host == "air-gp.example"
    assert config.port == 15432
    assert config.user == "air-user"
    assert config.password == "air-password"
    assert config.database == "air_db"
    assert config.connect_timeout == 8


def test_airflow_connections_file_allows_alias_different_from_connection_id(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, object]], Path],
) -> None:
    write_sql_connections(
        {
            "source": "airflow",
            "connections": {
                "trino": {
                    "connection_id": "AirTrino",
                    "type": "trino",
                    "insert_chunk_size": 400,
                }
            },
        }
    )
    install_fake_airflow(
        monkeypatch,
        {
            "AirTrino": FakeAirflowConnection(
                conn_type="trino",
                host="air-trino.example",
                port=8443,
                login="trino-user",
                password="trino-password",
                extra_dejson={"catalog": "iceberg", "schema": "sandbox"},
            )
        },
    )

    config = config_module.get_connection_config("trino")
    raw_connections = config_module.load_sql_connections()

    assert isinstance(config, config_module.TrinoConfig)
    assert config.connection_key == "trino"
    assert config.host == "air-trino.example"
    assert config.catalog == "iceberg"
    assert config.schema == "sandbox"
    assert config.insert_chunk_size == 400
    assert raw_connections["trino"]["host"] == "air-trino.example"
    assert raw_connections["trino"]["insert_chunk_size"] == 400


def test_airflow_connections_file_supports_public_transfer_options(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, object]], Path],
) -> None:
    write_sql_connections(
        {
            "source": "airflow",
            "connections": {
                "airflow_trino": {"type": "trino"},
                "airflow_gp": {"type": "gp"},
            },
        }
    )
    install_fake_airflow(
        monkeypatch,
        {
            "airflow_trino": FakeAirflowConnection(
                conn_type="trino",
                host="air-trino.example",
                login="trino-user",
                extra_dejson={"catalog": "iceberg", "schema": "sandbox"},
            ),
            "airflow_gp": FakeAirflowConnection(
                conn_type="postgres",
                host="air-gp.example",
                login="air-user",
                password="air-password",
                schema="air_db",
            ),
        },
    )

    options = api_module.build_transfer_options(
        from_db="airflow_trino",
        to_db="airflow_gp",
        from_sql="select 1",
        to_table="schema.target",
        gp_distributed_by_key=["id"],
    )

    assert options.from_db_key == "airflow_trino"
    assert options.from_db_backend == "trino"
    assert options.to_db_key == "airflow_gp"
    assert options.to_db_backend == "gp"
    assert options.gp_distributed_by_key == ["id"]


def test_airflow_connections_file_rejects_missing_type(
    write_sql_connections: Callable[[dict[str, object]], Path],
) -> None:
    write_sql_connections(
        {
            "source": "airflow",
            "connections": {"gp": {}},
        }
    )

    with pytest.raises(config_module.SqlConfigError, match="type"):
        config_module.get_connection_config("gp")


def test_airflow_connections_file_rejects_unknown_source(
    write_sql_connections: Callable[[dict[str, object]], Path],
) -> None:
    write_sql_connections(
        {
            "source": "vault",
            "connections": {},
        }
    )

    with pytest.raises(config_module.SqlConfigError, match="unsupported"):
        config_module.get_connection_config("gp")


def test_airflow_connections_file_rejects_malformed_connections(
    write_sql_connections: Callable[[dict[str, object]], Path],
) -> None:
    write_sql_connections(
        {
            "source": "airflow",
            "connections": [],
        }
    )

    with pytest.raises(config_module.SqlConfigError, match="connections"):
        config_module.get_connection_config("gp")


def test_airflow_connections_file_unknown_airflow_id_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, object]], Path],
) -> None:
    write_sql_connections(
        {
            "source": "airflow",
            "connections": {"gp": {"type": "gp"}},
        }
    )
    install_fake_airflow(monkeypatch, {})

    with pytest.raises(
        config_module.UnsupportedConnectionTypeError,
        match="Unknown Airflow connection ID: gp",
    ):
        config_module.get_connection_config("gp")


def test_direct_connections_file_can_still_have_source_alias(
    write_sql_connections: Callable[[dict[str, object]], Path],
) -> None:
    write_sql_connections(
        {
            "source": {
                "type": "gp",
                "host": "gp-source.example",
                "user": "user",
                "password": "password",
                "database": "db",
            }
        }
    )

    config = config_module.get_connection_config("source")

    assert isinstance(config, config_module.GpConfig)
    assert config.host == "gp-source.example"


def test_trino_connection_uses_airflow_file_timeout_and_source_overrides(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, object]], Path],
) -> None:
    write_sql_connections(
        {
            "source": "airflow",
            "connections": {
                "trino": {
                    "connection_id": "AirTrino",
                    "type": "trino",
                    "request_timeout": "900",
                    "source": "analytics_toolkit",
                }
            },
        }
    )
    install_fake_airflow(
        monkeypatch,
        {
            "AirTrino": FakeAirflowConnection(
                conn_type="trino",
                host="air-trino.example",
                port=8443,
                login="trino-user",
                password="trino-password",
                extra_dejson={
                    "catalog": "iceberg",
                    "schema": "sandbox",
                    "http_scheme": "https",
                    "verify": False,
                    "request_timeout": 600,
                    "source": "airflow",
                },
            )
        },
    )
    connect_calls: list[dict[str, object]] = []

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

    connection_module.get_sql_connection("trino")

    assert connect_calls == [
        {
            "host": "air-trino.example",
            "port": 8443,
            "user": "trino-user",
            "http_scheme": "https",
            "auth": connect_calls[0]["auth"],
            "verify": False,
            "catalog": "iceberg",
            "schema": "sandbox",
            "request_timeout": 900,
            "source": "analytics_toolkit",
        }
    ]
    auth = connect_calls[0]["auth"]
    assert isinstance(auth, FakeBasicAuthentication)
    assert auth.user == "trino-user"
    assert auth.password == "trino-password"


def test_airflow_file_extra_fallback_uses_airflow_extra_when_present(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, object]], Path],
) -> None:
    write_sql_connections(
        {
            "source": "airflow",
            "connections": {
                "trino": {
                    "connection_id": "AirTrino",
                    "type": "trino",
                    "http_scheme": {"from": "extra", "fallback": "https"},
                    "verify": {"from": "extra", "fallback": False},
                    "request_timeout": {"from": "extra", "fallback": 300},
                    "source": {
                        "from": "extra",
                        "key": "client_source",
                        "fallback": "airflow-trino",
                    },
                }
            },
        }
    )
    install_fake_airflow(
        monkeypatch,
        {
            "AirTrino": FakeAirflowConnection(
                conn_type="trino",
                host="air-trino.example",
                port=8443,
                login="trino-user",
                password="trino-password",
                extra_dejson={
                    "catalog": "iceberg",
                    "schema": "sandbox",
                    "http_scheme": "http",
                    "verify": True,
                    "request_timeout": 120,
                    "client_source": "airflow-extra",
                },
            )
        },
    )

    config = config_module.get_connection_config("trino")

    assert isinstance(config, config_module.TrinoConfig)
    assert config.http_scheme == "http"
    assert config.verify_value == "true"
    assert config.request_timeout == 120
    assert config.source == "airflow-extra"


def test_airflow_file_extra_fallback_uses_fallback_when_extra_missing(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, object]], Path],
) -> None:
    write_sql_connections(
        {
            "source": "airflow",
            "connections": {
                "trino": {
                    "connection_id": "AirTrino",
                    "type": "trino",
                    "http_scheme": {"from": "extra", "fallback": "https"},
                    "verify": {"from": "extra", "fallback": False},
                    "request_timeout": {"from": "extra", "fallback": 300},
                    "source": {"from": "extra", "fallback": "airflow-trino"},
                }
            },
        }
    )
    install_fake_airflow(
        monkeypatch,
        {
            "AirTrino": FakeAirflowConnection(
                conn_type="trino",
                host="air-trino.example",
                port=8443,
                login="trino-user",
                password="trino-password",
                extra_dejson={"catalog": "iceberg", "schema": "sandbox"},
            )
        },
    )
    connect_calls: list[dict[str, object]] = []

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

    connection_module.get_sql_connection("trino")

    assert connect_calls[0]["http_scheme"] == "https"
    assert connect_calls[0]["verify"] is False
    assert connect_calls[0]["request_timeout"] == 300
    assert connect_calls[0]["source"] == "airflow-trino"


@pytest.mark.parametrize(
    "resolver",
    [
        {"from": "env", "fallback": "https"},
        {"from": "extra", "key": 123, "fallback": "https"},
        {"from": "extra", "fallback": "https", "unexpected": True},
        {"from": "extra", "default": "https"},
    ],
)
def test_airflow_file_extra_fallback_rejects_malformed_resolver(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, object]], Path],
    resolver: dict[str, object],
) -> None:
    write_sql_connections(
        {
            "source": "airflow",
            "connections": {
                "trino": {
                    "connection_id": "AirTrino",
                    "type": "trino",
                    "http_scheme": resolver,
                }
            },
        }
    )
    install_fake_airflow(
        monkeypatch,
        {
            "AirTrino": FakeAirflowConnection(
                conn_type="trino",
                host="air-trino.example",
                login="trino-user",
            )
        },
    )

    with pytest.raises(config_module.SqlConfigError, match="resolver"):
        config_module.get_connection_config("trino")


def test_direct_connections_file_does_not_resolve_airflow_fallback_objects(
    write_sql_connections: Callable[[dict[str, object]], Path],
) -> None:
    write_sql_connections(
        {
            "trino": {
                "type": "trino",
                "host": "trino.example",
                "user": "user",
                "http_scheme": {"from": "extra", "fallback": "https"},
            }
        }
    )

    with pytest.raises(config_module.SqlConfigError, match="http_scheme"):
        config_module.get_connection_config("trino")


def test_transfer_options_allow_two_aliases_with_same_backend() -> None:
    options = api_module.build_transfer_options(
        from_db="gp",
        to_db="gp_sandbox",
        from_sql="select 1",
        to_table="schema.target",
    )

    assert options.from_db_key == "gp"
    assert options.from_db_backend == "gp"
    assert options.to_db_key == "gp_sandbox"
    assert options.to_db_backend == "gp"


def test_transfer_options_enable_clickhouse_host_drop_retry_by_default() -> None:
    options = api_module.build_transfer_options(
        from_db="trino",
        to_db="ch",
        from_sql="select 1",
        to_table="schema.target",
    )
    assert options.ch_retry_per_host_drops is True
    assert options.ch_retry_per_host_drops_concurrency == 5

    disabled = api_module.build_transfer_options(
        from_db="trino",
        to_db="ch",
        from_sql="select 1",
        to_table="schema.target",
        ch_retry_per_host_drops=False,
    )
    assert disabled.ch_retry_per_host_drops is False
    assert disabled.ch_retry_per_host_drops_concurrency is None

    custom = api_module.build_transfer_options(
        from_db="trino",
        to_db="ch",
        from_sql="select 1",
        to_table="schema.target",
        ch_retry_per_host_drops_concurrency=2,
    )
    assert custom.ch_retry_per_host_drops_concurrency == 2

    non_ch = api_module.build_transfer_options(
        from_db="trino",
        to_db="gp",
        from_sql="select 1",
        to_table="schema.target",
    )
    assert non_ch.ch_retry_per_host_drops is False
    assert non_ch.ch_retry_per_host_drops_concurrency is None

    with pytest.raises(ValueError, match="ch_retry_per_host_drops_concurrency"):
        api_module.build_transfer_options(
            from_db="trino",
            to_db="ch",
            from_sql="select 1",
            to_table="schema.target",
            ch_retry_per_host_drops_concurrency=0,
        )


def test_backend_specific_validation_uses_alias_backend(
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections(
        {
            "target_gp": {
                "type": "gp",
                "host": "gp.example",
                "user": "user",
                "password": "password",
                "database": "db",
            },
            "source_trino": {
                "type": "trino",
                "host": "trino.example",
                "user": "user",
            },
        }
    )

    options = api_module.build_transfer_options(
        from_db="source_trino",
        to_db="target_gp",
        from_sql="select 1",
        to_table="schema.target",
        gp_distributed_by_key=["id"],
    )

    assert options.to_db_key == "target_gp"
    assert options.to_db_backend == "gp"
    assert options.gp_distributed_by_key == ["id"]


def test_create_table_sql_accepts_connection_alias() -> None:
    sql = create_sql_table_module.build_create_table_sql(
        connection_type="gp_sandbox",
        table_name="schema.target",
        batch=pd.DataFrame({"id": [1], "value": ["x"]}),
        gp_distributed_by_key=["id"],
    )

    assert '"id" BIGINT' in sql
    assert 'DISTRIBUTED BY ("id")' in sql


def test_create_table_sql_accepts_table_schema_override() -> None:
    batch = pd.DataFrame({"id": [1], "amount": [10.5]})

    gp_sql = create_sql_table_module.build_create_table_sql(
        connection_type="gp",
        table_name="schema.target",
        batch=batch,
        table_schema={"id": "TEXT", "amount": "NUMERIC(10, 2)"},
    )
    trino_sql = create_sql_table_module.build_create_table_sql(
        connection_type="trino",
        table_name="schema.target",
        batch=batch,
        table_schema={"id": "VARCHAR", "amount": "DECIMAL(10, 2)"},
    )
    ch_sqls = create_sql_table_module.build_create_table_sqls(
        connection_type="ch",
        table_name="schema.target",
        batch=batch,
        table_schema={"id": "String", "amount": "Decimal(10, 2)"},
        ch_distributed_table=True,
    )

    assert '"id" TEXT' in gp_sql
    assert '"amount" NUMERIC(10, 2)' in gp_sql
    assert '"id" VARCHAR' in trino_sql
    assert '"amount" DECIMAL(10, 2)' in trino_sql
    assert any("`id` String" in sql for sql in ch_sqls)
    assert any("`amount` Decimal(10, 2)" in sql for sql in ch_sqls)


def test_trino_create_table_sql_accepts_partition_and_order_properties() -> None:
    sql = create_sql_table_module.build_create_table_sql(
        connection_type="trino",
        table_name="schema.target",
        batch=pd.DataFrame({"dt": ["2026-05-01"], "id": [1]}),
        partition_by=["dt"],
        order_by=["dt", "id"],
    )

    assert "format = 'PARQUET'" in sql
    assert "object_store_layout_enabled = true" in sql
    assert "partitioning = ARRAY['dt']" in sql
    assert "sorted_by = ARRAY['dt', 'id']" in sql


def test_gp_create_table_sql_accepts_partition_column_and_rejects_order() -> None:
    sql = create_sql_table_module.build_create_table_sql(
        connection_type="gp",
        table_name="schema.target",
        batch=pd.DataFrame({"dt": ["2026-05-01"], "id": [1]}),
        gp_distributed_by_key=["id"],
        partition_by="dt",
    )

    assert 'DISTRIBUTED BY ("id")' in sql
    assert 'PARTITION BY RANGE ("dt")' in sql

    with pytest.raises(ValueError, match="order_by is not supported"):
        create_sql_table_module.build_create_table_sql(
            connection_type="gp",
            table_name="schema.target",
            batch=pd.DataFrame({"dt": ["2026-05-01"], "id": [1]}),
            order_by=["id"],
        )


@pytest.mark.parametrize(
    ("table_schema", "match"),
    [
        ({"id": "BIGINT"}, "missing SQL type"),
        ({"id": "BIGINT", "amount": "DOUBLE", "extra": "TEXT"}, "not present"),
        ({"id": "BIGINT", "amount": " "}, "must not be empty"),
    ],
)
def test_create_table_sql_validates_table_schema(
    table_schema: dict[str, str],
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        create_sql_table_module.build_create_table_sql(
            connection_type="gp",
            table_name="schema.target",
            batch=pd.DataFrame({"id": [1], "amount": [10.5]}),
            table_schema=table_schema,
        )


def test_create_table_sql_rejects_invalid_table_schema_type() -> None:
    with pytest.raises(TypeError, match="table_schema"):
        create_sql_table_module.build_create_table_sql(
            connection_type="gp",
            table_name="schema.target",
            batch=pd.DataFrame({"id": [1]}),
            table_schema=[("id", "BIGINT")],
        )


def test_create_table_sql_rejects_conflicting_schema_aliases() -> None:
    with pytest.raises(ValueError, match="table_schema and column_types"):
        create_sql_table_module.build_create_table_sql(
            connection_type="gp",
            table_name="schema.target",
            batch=pd.DataFrame({"id": [1]}),
            column_types={"id": "BIGINT"},
            table_schema={"id": "TEXT"},
        )


def test_trino_insert_chunk_size_comes_from_connection_config(
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections(
        {
            "trino_batch": {
                "type": "trino",
                "host": "trino.example",
                "user": "user",
                "insert_chunk_size": 250,
            }
        }
    )

    config = config_module.get_connection_config("trino_batch")

    assert config.insert_chunk_size == 250
    assert (
        load_sql_table_module._get_trino_insert_chunk_size(None, "trino_batch")
        == 250
    )


def test_legacy_trino_insert_chunk_size_env_is_ignored(monkeypatch) -> None:
    monkeypatch.setenv("TRINO_INSERT_CHUNK_SIZE", "2")

    assert (
        load_sql_table_module._get_trino_insert_chunk_size(None, "trino")
        == load_sql_table_module.DEFAULT_TRINO_INSERT_CHUNK_SIZE
    )
