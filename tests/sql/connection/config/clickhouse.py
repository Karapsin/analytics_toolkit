from __future__ import annotations

from tests.sql._support.connection_config import (
    Callable,
    Path,
    SqlConfigError,
    UnsupportedConnectionTypeError,
    _write_cert,
    builtins,
    ch_config_module,
    config_module,
    connection_module,
    install_fake_airflow,
    pytest,
    replace,
    sys,
    types,
)


def test_clickhouse_airflow_ca_variable_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = replace(
        config_module.get_connection_config("ch"),
        ca_certs=[],
        ca_certs_variable="ch_ca",
    )

    real_import = builtins.__import__

    def reject_airflow_import(
        name: str,
        globals_: object = None,
        locals_: object = None,
        fromlist: object = (),
        level: int = 0,
    ) -> object:
        if name == "airflow.models.variable":
            message = "airflow unavailable"
            raise ImportError(message)
        return real_import(name, globals_, locals_, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", reject_airflow_import)
    with pytest.raises(SqlConfigError, match="Variable support is unavailable"):
        connection_module._resolve_ch_ca_certs(config)

    monkeypatch.setattr(builtins, "__import__", real_import)
    install_fake_airflow(monkeypatch, {}, {"ch_ca": " "})
    with pytest.raises(SqlConfigError, match="must be a non-empty string"):
        connection_module._resolve_ch_ca_certs(config)


def test_clickhouse_ca_certs_list_uses_generated_bundle(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    _write_cert(".certs/clickhouse-root.pem", "CLICKHOUSE ROOT\n")
    _write_cert(".certs/clickhouse-intermediate.pem", "CLICKHOUSE INTERMEDIATE\n")
    write_sql_connections(
        {
            "ch_ssl": {
                "type": "ch",
                "host": "ch.example",
                "user": "user",
                "password": "password",
                "ca_certs": ["clickhouse-root.pem", "clickhouse-intermediate.pem"],
            }
        }
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

    result = connection_module.get_sql_connection("ch_ssl")

    assert result is client
    bundle_path = Path(str(client_calls[0]["ca_cert"]))
    expected_bundle_path = Path.cwd() / ".certs" / ".generated" / "ch_ssl-ca-bundle.pem"
    assert bundle_path == expected_bundle_path
    assert bundle_path.read_text(encoding="utf-8") == "CLICKHOUSE ROOT\nCLICKHOUSE INTERMEDIATE\n"


def test_clickhouse_ca_certs_missing_file_fails_only_when_connecting(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections(
        {
            "ch_ssl": {
                "type": "ch",
                "host": "ch.example",
                "user": "user",
                "password": "password",
                "ca_certs": "missing.pem",
            }
        }
    )
    client_calls: list[dict[str, object]] = []
    fake_clickhouse_connect = types.SimpleNamespace(
        common=types.SimpleNamespace(set_setting=lambda name, value: None),
        get_client=lambda **kwargs: client_calls.append(kwargs) or object(),
    )
    monkeypatch.setitem(sys.modules, "clickhouse_connect", fake_clickhouse_connect)
    monkeypatch.setitem(
        sys.modules,
        "clickhouse_connect.common",
        fake_clickhouse_connect.common,
    )

    config = config_module.get_connection_config("ch_ssl")
    assert config.ca_certs == ["missing.pem"]
    with pytest.raises(config_module.SqlConfigError, match="missing certificate file"):
        connection_module.get_sql_connection("ch_ssl")
    assert client_calls == []


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


def test_clickhouse_connection_passes_optional_connector_settings(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    certs_dir = Path.cwd() / ".certs"
    certs_dir.mkdir()
    ca_path = certs_dir / "clickhouse-ca.pem"
    ca_path.write_text("CLICKHOUSE CA\n", encoding="utf-8")
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
                "ddl_ready_timeout_seconds": "900",
                "ddl_ready_timeout_extension_cnt": "4",
                "client_name": "analytics-toolkit",
            }
        }
    )
    install_fake_airflow(
        monkeypatch,
        {},
        variables={"clickhouse_ca_cert": "clickhouse-ca.pem"},
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
    assert config_module.get_connection_config("ch_custom").ddl_ready_timeout_seconds == 900
    assert config_module.get_connection_config("ch_custom").ddl_ready_timeout_extension_cnt == 4
    assert client_calls == [
        {
            "host": "ch.example",
            "port": 8123,
            "username": "user",
            "password": "password",
            "secure": True,
            "database": "default",
            "verify": False,
            "ca_cert": str(ca_path.resolve()),
            "connect_timeout": 11,
            "send_receive_timeout": 6001,
            "settings": {"connect_timeout": "500", "use_numpy": True},
            "interface": "https",
            "query_limit": 100,
            "query_retries": 4,
            "client_name": "analytics-toolkit",
        }
    ]


def test_clickhouse_driver_defaults_select_transport_ports(
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections(
        {
            "http": {
                "type": "ch",
                "host": "http.example",
                "user": "user",
                "password": "password",
            },
            "native": {
                "type": "ch",
                "driver": "native",
                "host": "native.example",
                "user": "user",
                "password": "password",
            },
        }
    )

    http = config_module.get_connection_config("http")
    native = config_module.get_connection_config("native")

    assert isinstance(http, config_module.ChConfig)
    assert http.driver == "http"
    assert http.port == 8123
    assert http.compression is False
    assert isinstance(native, config_module.ChConfig)
    assert native.driver == "native"
    assert native.port == 9000
    assert native.compression is False


def test_clickhouse_host_connection_validates_backend_host_and_dispatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gp_config = config_module.get_connection_config("gp")
    ch_config = config_module.get_connection_config("ch")
    opened: list[object] = []

    class Backend:
        def open_connection(self, config: object, **kwargs: object) -> object:
            opened.append((config, kwargs))
            return "connection"

    monkeypatch.setattr(connection_module, "get_backend", lambda backend: Backend())
    monkeypatch.setattr(
        connection_module,
        "get_connection_config",
        lambda key: gp_config if key == "gp" else ch_config,
    )

    with pytest.raises(UnsupportedConnectionTypeError, match="not a ClickHouse"):
        connection_module.get_ch_connection_for_host("gp", "host")
    with pytest.raises(ValueError, match="host must not be empty"):
        connection_module.get_ch_connection_for_host("ch", " ")

    assert connection_module.get_ch_connection_for_host("ch", " shard-1 ") == "connection"
    opened_config = opened[0][0]
    assert opened_config.host == "shard-1"


def test_clickhouse_missing_ca_certs_variable_is_clear_config_error(
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


def test_direct_clickhouse_connection_reports_blocked_connector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_import = builtins.__import__

    def blocked_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "clickhouse_connect" or name.startswith("clickhouse_connect."):
            message = "blocked"
            raise ImportError(message)
        return real_import(name, *args, **kwargs)

    config = types.SimpleNamespace(
        host="clickhouse.example",
        port=8443,
        user="analyst",
        password="secret",
        secure=True,
        database=None,
        verify_value=None,
        connect_timeout=None,
        send_receive_timeout=None,
        settings=None,
        interface=None,
        query_limit=None,
        query_retries=None,
        client_name=None,
    )
    with monkeypatch.context() as context:
        context.setattr(builtins, "__import__", blocked_import)
        context.delitem(sys.modules, "clickhouse_connect", raising=False)
        context.delitem(sys.modules, "clickhouse_connect.common", raising=False)
        with pytest.raises(ImportError, match="required for ClickHouse"):
            ch_config_module.open_connection(
                config,
                parse_verify_value=lambda value: value,
                resolve_ch_ca_certs=lambda _config: None,
            )


@pytest.mark.parametrize("compression", [1, "gzip"])
def test_invalid_clickhouse_compression_is_connection_specific(
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
    compression: object,
) -> None:
    write_sql_connections(
        {
            "bad_compression": {
                "type": "ch",
                "driver": "native",
                "host": "native.example",
                "user": "user",
                "password": "password",
                "compression": compression,
            }
        }
    )

    with pytest.raises(SqlConfigError, match=r"bad_compression.*compression"):
        config_module.get_connection_config("bad_compression")


def test_invalid_clickhouse_driver_is_connection_specific(
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections(
        {
            "bad_driver": {
                "type": "ch",
                "driver": "tcp",
                "host": "native.example",
                "user": "user",
                "password": "password",
            }
        }
    )

    with pytest.raises(SqlConfigError, match=r"bad_driver.*driver.*http.*native"):
        config_module.get_connection_config("bad_driver")


def test_native_clickhouse_constructor_and_lazy_airflow_ca(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    ca_path = _write_cert(".certs/native-ca.pem")
    write_sql_connections(
        {
            "native": {
                "type": "ch",
                "driver": "native",
                "host": "native.example",
                "port": 9003,
                "user": "native-user",
                "password": "native-password",
                "database": "analytics",
                "secure": True,
                "verify": False,
                "ca_certs_variable": "ca_certificate",
                "connect_timeout": 11,
                "send_receive_timeout": 6000,
                "settings": {"connect_timeout": "500"},
                "compression": "zstd",
                "client_name": "analytics-toolkit",
            }
        }
    )
    install_fake_airflow(monkeypatch, {}, variables={"ca_certificate": "native-ca.pem"})
    calls: list[dict[str, object]] = []

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            calls.append(kwargs)

    fake_module = types.ModuleType("clickhouse_driver")
    fake_module.Client = FakeClient
    monkeypatch.setitem(sys.modules, "clickhouse_driver", fake_module)

    config = config_module.get_connection_config("native")
    assert isinstance(config, config_module.ChConfig)
    assert calls == []
    result = connection_module.get_sql_connection("native")

    assert result.__class__.__name__ == "NativeClickHouseClient"
    assert calls == [
        {
            "host": "native.example",
            "port": 9003,
            "user": "native-user",
            "password": "native-password",
            "database": "analytics",
            "secure": True,
            "verify": False,
            "ca_certs": str(ca_path.resolve()),
            "connect_timeout": 11,
            "send_receive_timeout": 6000,
            "settings": {"connect_timeout": "500"},
            "compression": "zstd",
            "client_name": "analytics-toolkit",
        }
    ]


def test_native_clickhouse_constructor_omits_unconfigured_options(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections(
        {
            "native": {
                "type": "ch",
                "driver": "native",
                "host": "native.example",
                "user": "user",
                "password": "password",
            }
        }
    )
    calls: list[dict[str, object]] = []

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            calls.append(kwargs)

    fake_module = types.ModuleType("clickhouse_driver")
    fake_module.Client = FakeClient
    monkeypatch.setitem(sys.modules, "clickhouse_driver", fake_module)

    connection_module.get_sql_connection("native")

    assert calls == [
        {
            "host": "native.example",
            "port": 9000,
            "user": "user",
            "password": "password",
            "database": "",
            "secure": False,
            "compression": False,
        }
    ]


@pytest.mark.parametrize("field", ["interface", "query_limit", "query_retries"])
def test_native_clickhouse_rejects_http_only_fields(
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
    field: str,
) -> None:
    write_sql_connections(
        {
            "native_bad": {
                "type": "ch",
                "driver": "native",
                "host": "native.example",
                "user": "user",
                "password": "password",
                field: 1,
            }
        }
    )

    with pytest.raises(SqlConfigError, match=r"native_bad.*HTTP-only"):
        config_module.get_connection_config("native_bad")
