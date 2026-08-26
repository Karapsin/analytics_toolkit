from __future__ import annotations

from tests.sql._support.connection_config import (
    _MISSING,
    Callable,
    FakeAirflowConnection,
    Path,
    SqlConfigError,
    UnsupportedConnectionTypeError,
    config_module,
    connection_module,
    install_fake_airflow,
    pytest,
    sys,
    types,
)


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


def test_airflow_file_parser_rejects_invalid_entries(tmp_path: Path) -> None:
    path = tmp_path / ".connections"
    with pytest.raises(SqlConfigError, match="keys must be strings"):
        config_module._parse_airflow_connections_file(
            {"connections": {1: {}}},
            path,
        )
    with pytest.raises(SqlConfigError, match="must be a JSON object"):
        config_module._parse_airflow_connections_file(
            {"connections": {"gp": []}},
            path,
        )
    with pytest.raises(SqlConfigError, match="Duplicate SQL connection key"):
        config_module._parse_airflow_connections_file(
            {
                "connections": {
                    "GP": {"type": "gp"},
                    " gp ": {"type": "gp"},
                }
            },
            path,
        )


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


def test_airflow_raw_json_extra_and_boolean_type_edges() -> None:
    connection = FakeAirflowConnection(
        extra_dejson=_MISSING,
        extra='{"catalog": "iceberg"}',
    )
    assert config_module._get_airflow_connection_extras(connection, "AirTrino") == {
        "catalog": "iceberg"
    }
    with pytest.raises(SqlConfigError, match="must be a boolean"):
        config_module._optional_bool({"flag": 1}, "gp", "flag", False)


def test_airflow_source_entry_normalization_and_unknown_key() -> None:
    entry = config_module._AirflowConnectionEntry(
        connection_id="AirGP",
        backend="gp",
        overrides={},
    )
    source = config_module._AirflowConnectionSource(
        connections={"AirGP": entry},
        normalized_connections={"airgp": "AirGP"},
        default_backend=None,
    )

    assert (
        config_module._get_airflow_source_entry(
            source,
            " airGP ",
            allow_dynamic=False,
        )
        is entry
    )
    with pytest.raises(UnsupportedConnectionTypeError, match="Available keys"):
        config_module._get_airflow_source_entry(
            source,
            "missing",
            allow_dynamic=False,
        )


def test_airflow_source_native_clickhouse_keeps_explicit_port_and_options(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, object]], Path],
) -> None:
    write_sql_connections(
        {
            "source": "airflow",
            "connections": {
                "clickhouse_pa_core": {
                    "type": "ch",
                    "driver": "native",
                    "compression": {"from": "extra", "fallback": False},
                    "ca_certs_variable": "ca_certificate",
                    "send_receive_timeout": 6000,
                    "settings": {"connect_timeout": "500"},
                }
            },
        }
    )
    install_fake_airflow(
        monkeypatch,
        {
            "clickhouse_pa_core": FakeAirflowConnection(
                conn_type="clickhouse",
                host="native.example",
                port=9003,
                login="native-user",
                password="native-password",
                schema="analytics",
                extra_dejson={"compression": "lz4"},
            )
        },
    )

    config = config_module.get_connection_config("clickhouse_pa_core")

    assert isinstance(config, config_module.ChConfig)
    assert config.driver == "native"
    assert config.port == 9003
    assert config.compression == "lz4"
    assert config.settings == {"connect_timeout": "500"}


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


def test_airflow_trino_connections_file_supports_s3_transfer_staging_location(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, object]], Path],
) -> None:
    write_sql_connections(
        {
            "source": "airflow",
            "connections": {
                "airflow_trino": {
                    "type": "trino",
                    "transfer_staging_schema": "object_storage.sandbox",
                    "s3_transfer_staging_schema": "hive.sandbox",
                    "s3_transfer_staging_location": {
                        "from": "extra",
                        "key": "parquet_transfer_location",
                    },
                },
            },
        }
    )
    install_fake_airflow(
        monkeypatch,
        {
            "airflow_trino": FakeAirflowConnection(
                conn_type="trino",
                host="air-trino.example",
                login="air-user",
                password="air-password",
                extra_dejson={
                    "parquet_transfer_location": ("s3://bucket/tmp/analytics_toolkit_transfer"),
                },
            ),
        },
    )

    config = config_module.get_connection_config("airflow_trino")
    assert config.transfer_staging_schema == "object_storage.sandbox"
    assert config.s3_transfer_staging_schema == "hive.sandbox"
    assert config.s3_transfer_staging_location == "s3://bucket/tmp/analytics_toolkit_transfer"


def test_airflow_validation_and_backend_resolution_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_airflow(
        monkeypatch,
        {
            "AirGP": FakeAirflowConnection(
                conn_type="gp",
                host="gp.example",
                login="user",
                password="password",
                schema="db",
            )
        },
    )

    with config_module.use_airflow_connections(connection_backends={"AirGP": "gp"}):
        results = config_module.validate_connections([" AirGP "])
        assert results[0].connection_key == "AirGP"
        assert config_module.resolve_connection_backend("AirGP") == "gp"

    monkeypatch.setattr(config_module, "get_connection_backend", lambda key: "trino")
    assert config_module.resolve_connection_backend("warehouse") == "trino"
