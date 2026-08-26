from __future__ import annotations

from tests.sql._support.connection_config import (
    _MISSING,
    Callable,
    FakeAirflowConnection,
    Path,
    SqlConfigError,
    api_module,
    builtins,
    config_module,
    connection_module,
    install_fake_airflow,
    pytest,
    sys,
    types,
)


def test_airflow_backend_id_and_resolver_edges() -> None:
    with pytest.raises(SqlConfigError, match="does not define a backend"):
        config_module._resolve_airflow_connection_backend(
            FakeAirflowConnection(),
            {},
            "id",
        )
    with pytest.raises(SqlConfigError, match="ID must not be empty"):
        config_module._normalize_airflow_connection_id(" ")

    assert config_module._is_airflow_extra_resolver(
        "settings",
        {"from": "extra", "default": 1, "custom": 2},
    )
    assert (
        config_module._resolve_airflow_entry_overrides(
            {"port": {"from": "extra", "key": "missing"}},
            {},
            "id",
        )
        == {}
    )


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
    assert config.ca_certs == []
    assert config.ca_certs_variable == "clickhouse_ca_cert"
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
                    "ca_certs": "/local/ca.pem",
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
    assert config.ca_certs == ["/local/ca.pem"]
    assert config.ca_certs_variable is None


def test_airflow_connection_config_requires_airflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for module_name in ("airflow.hooks.base", "airflow.hooks", "airflow"):
        monkeypatch.delitem(sys.modules, module_name, raising=False)

    real_import = builtins.__import__

    def fake_import(
        name: str,
        globals_: object | None = None,
        locals_: object | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        if name.startswith("airflow"):
            message = "airflow is unavailable"
            raise ImportError(message)
        return real_import(name, globals_, locals_, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(config_module.SqlConfigError, match="apache-airflow"):
        config_module.airflow_connection_config("missing", "gp")


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


def test_airflow_connections_file_supports_transfer_staging_schema(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, object]], Path],
) -> None:
    write_sql_connections(
        {
            "source": "airflow",
            "connections": {
                "airflow_gp": {"type": "gp", "transfer_staging_schema": "airflow_transfer_gp"},
                "airflow_trino": {
                    "type": "trino",
                    "transfer_staging_schema": "airflow_transfer_trino",
                },
                "airflow_ch": {"type": "ch", "transfer_staging_schema": "airflow_transfer_ch"},
            },
        }
    )
    install_fake_airflow(
        monkeypatch,
        {
            "airflow_gp": FakeAirflowConnection(
                conn_type="postgres",
                host="air-gp.example",
                login="air-user",
                password="air-password",
                schema="air_db",
            ),
            "airflow_trino": FakeAirflowConnection(
                conn_type="trino",
                host="air-trino.example",
                login="air-user",
                password="air-password",
            ),
            "airflow_ch": FakeAirflowConnection(
                conn_type="clickhouse",
                host="air-ch.example",
                login="ch-user",
                password="ch-password",
                schema="default",
            ),
        },
    )

    assert config_module.get_connection_config("airflow_gp").transfer_staging_schema == (
        "airflow_transfer_gp"
    )
    assert config_module.get_connection_config("airflow_trino").transfer_staging_schema == (
        "airflow_transfer_trino"
    )
    assert config_module.get_connection_config("airflow_ch").transfer_staging_schema == (
        "airflow_transfer_ch"
    )


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


@pytest.mark.parametrize(
    ("connection", "message"),
    [
        (FakeAirflowConnection(extra_dejson=[]), "extra_dejson must be a dict"),
        (FakeAirflowConnection(extra_dejson=_MISSING, extra={"x": 1}), None),
        (FakeAirflowConnection(extra_dejson=_MISSING, extra="{"), "valid JSON"),
        (FakeAirflowConnection(extra_dejson=_MISSING, extra="[]"), "JSON object"),
        (FakeAirflowConnection(extra_dejson=_MISSING, extra=1), "JSON object"),
    ],
)
def test_airflow_extra_shapes(
    connection: FakeAirflowConnection,
    message: str | None,
) -> None:
    if message is None:
        assert config_module._get_airflow_connection_extras(connection, "id") == {"x": 1}
        return
    with pytest.raises(SqlConfigError, match=message):
        config_module._get_airflow_connection_extras(connection, "id")


@pytest.mark.parametrize(
    "resolver",
    [
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
