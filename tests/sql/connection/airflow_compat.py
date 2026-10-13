from __future__ import annotations

import sys
from pathlib import Path

import pytest
from analytics_toolkit import general
from analytics_toolkit.sql.connection import airflow_compat, config
from analytics_toolkit.sql.connection.errors import SqlConfigError, UnsupportedConnectionTypeError
from analytics_toolkit.sql.connection.get_sql_connection import _resolve_ch_ca_certs
from analytics_toolkit.sql.connection.references import _read_airflow_variable

from tests.sql._support.connection_config import FakeAirflowConnection, install_fake_airflow


@pytest.mark.parametrize("major", [2, 3])
def test_airflow_majors_resolve_connections_variables_and_certificates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    major: int,
) -> None:
    connection = FakeAirflowConnection(
        conn_type="http",
        host="127.0.0.1",
        login="user",
        password="first",
        schema="db",
        extra_dejson={"driver": "native"},
    )
    pem = "-----BEGIN CERTIFICATE-----\nCERT\n-----END CERTIFICATE-----\n"
    (tmp_path / ".certs").mkdir()
    (tmp_path / ".certs/ca.crt").write_text(pem)
    install_fake_airflow(
        monkeypatch, {"warehouse": connection}, {"host": "localhost", "cert": "ca.crt"}, major=major
    )
    if major == 3:
        monkeypatch.setitem(sys.modules, "airflow.hooks.base", None)
        monkeypatch.setitem(sys.modules, "airflow.models.variable", None)
    routing = tmp_path / ".connections"
    routing.write_text(
        '{"source":"airflow","connections":{"warehouse":{"type":"ch","ca_certs_variable":"cert"}}}'
    )
    general.set_connections_path(routing)
    first = config.get_connection_config("warehouse")
    assert first.host == "127.0.0.1"
    assert _read_airflow_variable("warehouse", "host", "host") == "localhost"
    assert first.password == "first"
    assert first.driver == "native"
    certificate = _resolve_ch_ca_certs(first)
    assert certificate is not None
    assert Path(certificate).read_text().strip() == pem.strip()
    connection.password = "second"
    assert config.get_connection_config("warehouse").password == "second"


@pytest.mark.parametrize("major", [2, 3])
def test_airflow_lookup_errors_keep_config_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    major: int,
) -> None:
    install_fake_airflow(monkeypatch, {}, major=major)
    with pytest.raises(
        UnsupportedConnectionTypeError, match="Unknown Airflow connection ID: missing"
    ):
        config.airflow_connection_config("missing", "ch")
    error = pytest.raises(SqlConfigError, match="Could not resolve Airflow Variable")
    with config.use_airflow_connections(), error:
        _read_airflow_variable("warehouse", "host", "missing")


def test_airflow3_lookup_failure_does_not_fall_back_to_airflow2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_airflow(monkeypatch, {}, major=3)
    monkeypatch.setitem(sys.modules, "airflow.sdk", None)
    with pytest.raises(SqlConfigError, match="requires the 'apache-airflow'"):
        config.airflow_connection_config("missing", "ch")


def test_missing_and_unsupported_airflow_versions_fail_explicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_airflow(monkeypatch, {}, major=4)
    with pytest.raises(ImportError, match="Unsupported Airflow major"):
        airflow_compat.variable_getter()
    monkeypatch.setitem(sys.modules, "airflow", None)
    with pytest.raises(ImportError):
        airflow_compat.connection_getter()
