from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend
from analytics_toolkit.sql.backends.ch.recovery_endpoints import RecoveryEndpoint

from tests.sql._support.connection_config import (
    FakeAirflowConnection,
    _expected_trino_read_options,
    _install_fake_trino,
    config_module,
    connection_module,
    install_fake_airflow,
)


@pytest.mark.parametrize("backend", ["gp", "ch", "trino"])
@pytest.mark.parametrize("password", ["", "  ", " leading and trailing \t"])
@pytest.mark.parametrize("source", ["direct", ".secrets", "env", "airflow_variable"])
def test_password_contents_survive_all_resolution_paths(  # noqa: PLR0913 -- source matrix and isolated project fixtures.
    backend, password, source, monkeypatch, tmp_path, write_sql_connections
):
    raw = {"type": backend, "host": " host ", "user": " user ", "database": "db"}
    if source == "direct":
        raw["password"] = password
    else:
        raw["password"] = {"from": source, "key": "PASSWORD"}
        if source == ".secrets":
            secret_path = tmp_path / ".secrets"
            secret_path.write_text(f"PASSWORD='{password}'\n", encoding="utf-8")
            secret_path.chmod(0o600)
        elif source == "env":
            monkeypatch.setenv("PASSWORD", password)
        else:
            install_fake_airflow(monkeypatch, {}, {"PASSWORD": password})
    write_sql_connections({"target": raw})
    captured = []
    adapter = get_backend(backend)

    def open_connection(_adapter, config, **_kwargs):
        captured.append(config)
        return object()

    monkeypatch.setattr(type(adapter), "open_connection", open_connection)
    sql.get_sql_connection("target")
    assert captured[0].password == password
    assert captured[0].host == "host"
    assert captured[0].user == "user"


@pytest.mark.parametrize("password", ["", "  password  "])
def test_airflow_connection_password_is_not_trimmed(password, monkeypatch, write_sql_connections):
    install_fake_airflow(
        monkeypatch,
        {
            "air": FakeAirflowConnection(
                conn_type="postgres", host="host", login="user", password=password, schema="db"
            )
        },
    )
    with config_module.use_airflow_connections({"air": "gp"}):
        assert config_module.get_connection_config("air").password == password


def test_explicit_empty_trino_password_uses_basic_auth(monkeypatch, write_sql_connections):
    write_sql_connections(
        {"target": {"type": "trino", "host": "host", "user": "user", "password": ""}}
    )
    calls = []
    _install_fake_trino(monkeypatch, calls)
    sql.get_sql_connection("target")
    assert calls[0]["auth"].password == ""
    for name, value in _expected_trino_read_options().items():
        assert calls[0][name] == value


@pytest.mark.parametrize("value", ["cert.pem", "truthy", "", 1, "yes", "0"])
def test_clickhouse_verify_rejects_non_boolean_configuration(value, write_sql_connections):
    write_sql_connections(
        {"ch": {"type": "ch", "host": "host", "user": "user", "password": "", "verify": value}}
    )
    result = sql.validate_connections(["ch"])[0]
    assert not result.valid
    assert "ca_certs" in result.error


@pytest.mark.parametrize(
    ("value", "expected"),
    [(True, "true"), (False, "false"), (" TRUE ", "true"), ("false", "false")],
)
def test_clickhouse_verify_accepts_boolean_forms(value, expected, write_sql_connections):
    write_sql_connections(
        {"ch": {"type": "ch", "host": "host", "user": "user", "password": "", "verify": value}}
    )
    assert config_module.get_connection_config("ch").verify_value == expected


def test_trino_retains_verify_ca_path(write_sql_connections):
    write_sql_connections(
        {"trino": {"type": "trino", "host": "host", "user": "user", "verify": "/ca/trino.pem"}}
    )
    assert config_module.get_connection_config("trino").verify_value == "/ca/trino.pem"


def test_certificate_bundles_are_atomic_content_addressed_and_immutable(tmp_path):
    certificate_dir = tmp_path / ".certs"
    certificate_dir.mkdir()
    first = certificate_dir / "first.pem"
    second = certificate_dir / "second.pem"
    first.write_bytes(b"FIRST\n")
    second.write_bytes(b"SECOND\n")

    def resolve(_index):
        return connection_module._resolve_ca_certs("same", ["first.pem", "second.pem"])

    with ThreadPoolExecutor(max_workers=8) as executor:
        paths = list(executor.map(resolve, range(32)))
    assert len(set(paths)) == 1
    previous = Path(paths[0])
    assert previous.read_bytes() == b"FIRST\nSECOND\n"
    first.write_bytes(b" FIRST \n\n")
    replacement = Path(resolve(0))
    assert replacement != previous
    assert previous.read_bytes() == b"FIRST\nSECOND\n"
    assert replacement.read_bytes() == b" FIRST \n\nSECOND\n"
    assert not list(replacement.parent.glob(".ca-bundle-*"))


@pytest.mark.parametrize(("driver", "expected"), [("native", 9100), ("http", 8123)])
def test_recovery_uses_discovered_native_port_only(
    driver, expected, monkeypatch, write_sql_connections
):

    write_sql_connections(
        {
            "ch": {
                "type": "ch",
                "host": "coordinator",
                "user": "user",
                "password": "",
                "driver": driver,
            }
        }
    )
    captured = []
    adapter = get_backend("ch")
    monkeypatch.setattr(
        type(adapter),
        "open_connection",
        lambda _adapter, config, **_kwargs: captured.append(config) or object(),
    )
    connection_module.get_ch_connection_for_host("ch", RecoveryEndpoint("node", 9100))
    assert captured[0].host == "node"
    assert captured[0].port == expected
