from __future__ import annotations

from tests.sql._support.connection_config import (
    Callable,
    Path,
    _write_cert,
    config_module,
    connection_module,
    create_sql_table_module,
    pd,
    pytest,
    sys,
    types,
)


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


def test_gp_create_table_sql_accepts_initial_partitions_and_rejects_order() -> None:
    sql = create_sql_table_module.create_sql_table(
        db_key="gp",
        table_name="schema.target",
        df=pd.DataFrame({"dt": ["2026-05-01"], "id": [1]}),
        gp_distributed_by_key=["id"],
        partition_by="dt",
        gp_partitions={
            "start": "2026-05-01",
            "end": "2026-07-01",
            "interval": "1 month",
        },
        only_generate_sql=True,
    )

    assert 'DISTRIBUTED BY ("id")' in sql
    assert 'PARTITION BY RANGE ("dt")' in sql
    assert "EVERY (INTERVAL '1 month')" in sql

    with pytest.raises(ValueError, match="order_by is not supported"):
        create_sql_table_module.create_sql_table(
            db_key="gp",
            table_name="schema.target",
            df=pd.DataFrame({"dt": ["2026-05-01"], "id": [1]}),
            order_by=["id"],
            only_generate_sql=True,
        )


def test_greenplum_ca_certs_missing_file_fails_only_when_connecting(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections(
        {
            "gp_ssl": {
                "type": "gp",
                "host": "gp.example",
                "user": "user",
                "password": "password",
                "database": "db",
                "ca_certs": "missing.pem",
            }
        }
    )
    connect_calls: list[dict[str, object]] = []
    fake_psycopg2 = types.SimpleNamespace(
        connect=lambda **kwargs: connect_calls.append(kwargs) or object()
    )
    monkeypatch.setitem(sys.modules, "psycopg2", fake_psycopg2)

    config = config_module.get_connection_config("gp_ssl")
    assert config.ca_certs == ["missing.pem"]
    with pytest.raises(config_module.SqlConfigError, match="missing certificate file"):
        connection_module.get_sql_connection("gp_ssl")
    assert connect_calls == []


def test_greenplum_connection_passes_ssl_cert_options(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    ca_path = _write_cert(".certs/gp-ca.pem", "GP CA\n")
    client_cert = _write_cert(".certs/client.pem", "CLIENT CERT\n")
    client_key = _write_cert(".certs/client.key", "CLIENT KEY\n")
    write_sql_connections(
        {
            "gp_ssl": {
                "type": "gp",
                "host": "gp.example",
                "user": "user",
                "password": "password",
                "database": "db",
                "ca_certs": "gp-ca.pem",
                "ssl_cert": ".certs/client.pem",
                "ssl_key": ".certs/client.key",
            }
        }
    )
    connect_calls: list[dict[str, object]] = []
    fake_psycopg2 = types.SimpleNamespace(
        connect=lambda **kwargs: connect_calls.append(kwargs) or object()
    )
    monkeypatch.setitem(sys.modules, "psycopg2", fake_psycopg2)

    config = config_module.get_connection_config("gp_ssl")
    connection_module.get_sql_connection("gp_ssl")

    assert config.sslmode == "verify-full"
    assert config.ca_certs == ["gp-ca.pem"]
    assert connect_calls[0]["sslmode"] == "verify-full"
    assert connect_calls[0]["sslrootcert"] == str(ca_path.resolve())
    assert connect_calls[0]["sslcert"] == str(client_cert.resolve())
    assert connect_calls[0]["sslkey"] == str(client_key.resolve())


def test_greenplum_connection_respects_explicit_sslmode(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    _write_cert(".certs/gp-ca.pem")
    write_sql_connections(
        {
            "gp_ssl": {
                "type": "gp",
                "host": "gp.example",
                "user": "user",
                "password": "password",
                "database": "db",
                "sslmode": "verify-ca",
                "ca_certs": "gp-ca.pem",
            }
        }
    )
    connect_calls: list[dict[str, object]] = []
    fake_psycopg2 = types.SimpleNamespace(
        connect=lambda **kwargs: connect_calls.append(kwargs) or object()
    )
    monkeypatch.setitem(sys.modules, "psycopg2", fake_psycopg2)

    connection_module.get_sql_connection("gp_ssl")

    assert connect_calls[0]["sslmode"] == "verify-ca"
