from __future__ import annotations

from tests.sql._support.connection_config import (
    Callable,
    FakeAirflowConnection,
    Path,
    SqlConfigError,
    _direct_trino_config,
    _install_fake_trino,
    _write_cert,
    builtins,
    config_module,
    connection_module,
    create_sql_table_module,
    install_fake_airflow,
    load_sql_table_module,
    pd,
    pytest,
    sys,
    trino_config_module,
    types,
)


def test_direct_trino_connection_import_auth_and_kwargs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_import = builtins.__import__

    def blocked_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "trino" or name.startswith("trino."):
            message = "blocked"
            raise ImportError(message)
        return real_import(name, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(builtins, "__import__", blocked_import)
        context.delitem(sys.modules, "trino", raising=False)
        context.delitem(sys.modules, "trino.auth", raising=False)
        with pytest.raises(ImportError, match="required for Trino"):
            trino_config_module.open_connection(
                _direct_trino_config(),
                parse_verify_value=lambda value: value,
                resolve_ca_certs=lambda *_args: None,
            )

    calls: list[dict[str, object]] = []
    _install_fake_trino(monkeypatch, calls)
    assert (
        trino_config_module.open_connection(
            _direct_trino_config(),
            parse_verify_value=lambda value: value == "/ca.pem",
            resolve_ca_certs=lambda *_args: "/ca.pem",
        )
        is not None
    )
    kwargs = calls[-1]
    assert kwargs["verify"] is True
    assert kwargs["catalog"] == "iceberg"
    assert kwargs["schema"] == "analytics"
    assert kwargs["request_timeout"] == 17
    assert kwargs["source"] == "coverage"
    assert kwargs["auth"].password == "secret"

    trino_config_module.open_connection(
        _direct_trino_config(
            auth_mode="oauth2",
            password=None,
            catalog=None,
            schema=None,
            request_timeout=None,
            source=None,
        ),
        parse_verify_value=lambda value: value,
        resolve_ca_certs=lambda *_args: None,
    )
    assert calls[-1]["auth"] is not None
    assert "catalog" not in calls[-1]
    assert "source" not in calls[-1]

    trino_config_module.open_connection(
        _direct_trino_config(password=None),
        parse_verify_value=lambda value: value,
        resolve_ca_certs=lambda *_args: None,
    )
    assert calls[-1]["auth"] is None
    with pytest.raises(SqlConfigError, match="unsupported auth_mode"):
        trino_config_module.open_connection(
            _direct_trino_config(auth_mode="kerberos"),
            parse_verify_value=lambda value: value,
            resolve_ca_certs=lambda *_args: None,
        )


def test_direct_trino_connections_file_supports_s3_transfer_staging_location(
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections(
        {
            "trino_with_location": {
                "type": "trino",
                "host": "trino.example",
                "user": "user",
                "password": "password",
                "catalog": "iceberg",
                "schema": "sandbox",
                "transfer_staging_schema": "object_storage.sandbox",
                "s3_transfer_staging_schema": "hive.sandbox",
                "s3_transfer_staging_location": "s3://bucket/tmp/analytics_toolkit_transfer",
            },
        }
    )

    config = config_module.get_connection_config("trino_with_location")
    assert config.transfer_staging_schema == "object_storage.sandbox"
    assert config.s3_transfer_staging_schema == "hive.sandbox"
    assert config.s3_transfer_staging_location == "s3://bucket/tmp/analytics_toolkit_transfer"


def test_legacy_trino_insert_chunk_size_env_is_ignored(monkeypatch) -> None:
    from analytics_toolkit.sql.backends.trino.insert import (
        DEFAULT_TRINO_INSERT_CHUNK_SIZE,
    )

    monkeypatch.setenv("TRINO_INSERT_CHUNK_SIZE", "2")

    assert (
        load_sql_table_module._get_trino_insert_chunk_size(None, "trino")
        == DEFAULT_TRINO_INSERT_CHUNK_SIZE
    )


def test_trino_ca_certs_missing_file_fails_only_when_connecting(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections(
        {
            "trino_ssl": {
                "type": "trino",
                "host": "trino.example",
                "user": "user",
                "ca_certs": "missing.pem",
            }
        }
    )
    connect_calls: list[dict[str, object]] = []
    _install_fake_trino(monkeypatch, connect_calls)

    config = config_module.get_connection_config("trino_ssl")
    assert config.ca_certs == ["missing.pem"]
    with pytest.raises(config_module.SqlConfigError, match="missing certificate file"):
        connection_module.get_sql_connection("trino_ssl")
    assert connect_calls == []


def test_trino_ca_certs_resolves_bare_name_and_overrides_verify(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    ca_path = _write_cert(".certs/trino-ca.pem", "TRINO CA\n")
    write_sql_connections(
        {
            "trino_ssl": {
                "type": "trino",
                "host": "trino.example",
                "user": "user",
                "verify": False,
                "ca_certs": "trino-ca.pem",
            }
        }
    )
    connect_calls: list[dict[str, object]] = []
    _install_fake_trino(monkeypatch, connect_calls)

    config = config_module.get_connection_config("trino_ssl")
    connection_module.get_sql_connection("trino_ssl")

    assert config.verify_value == "false"
    assert config.ca_certs == ["trino-ca.pem"]
    assert connect_calls[0]["verify"] == str(ca_path.resolve())


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


def test_trino_create_table_sql_accepts_partition_and_order_properties() -> None:
    sql = create_sql_table_module.create_table(
        db_key="trino",
        table_name="schema.target",
        df=pd.DataFrame({"dt": ["2026-05-01"], "id": [1]}),
        partition_by=["dt"],
        order_by=["dt", "id"],
        only_generate_sql=True,
    )

    assert "format = 'PARQUET'" in sql
    assert "object_store_layout_enabled = true" in sql
    assert "partitioning = ARRAY['dt']" in sql
    assert "sorted_by = ARRAY['dt', 'id']" in sql


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
    assert load_sql_table_module._get_trino_insert_chunk_size(None, "trino_batch") == 250
