"""Configuration-only public validation never reflects provider credentials."""

from __future__ import annotations

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.connection import validation as validation_module
from analytics_toolkit.sql.connection.errors import (
    SqlConfigError,
    UnsupportedConnectionTypeError,
)

from tests.sql._support.connection_config import config_module, install_fake_airflow


@pytest.mark.parametrize("major", [2, 3])
def test_airflow_provider_failure_is_sanitized_during_configuration_validation(
    monkeypatch, major, tmp_path
):
    config_module.get_connection_config("gp")
    install_fake_airflow(monkeypatch, {}, major=major)
    secret = "private-secret-provider-password"
    private_dsn = "postgresql://user:" + secret + "@private-host/database"

    def provider_failure(_connection_id):
        raise RuntimeError(private_dsn)

    monkeypatch.setattr(config_module, "connection_getter", lambda: provider_failure)
    with config_module.use_airflow_connections():
        result = sql.validate_connections(["warehouse"], connect=False)[0]
    assert not result.valid
    assert result.connected is None
    assert secret not in result.error
    assert private_dsn not in result.error
    assert str(tmp_path / ".connections") not in result.error
    assert "UnsupportedConnectionTypeError" in result.error


@pytest.mark.parametrize("error_type", [SqlConfigError, UnsupportedConnectionTypeError])
@pytest.mark.parametrize("chained", [False, True], ids=["context", "cause"])
@pytest.mark.parametrize("connect", [False, True])
def test_validation_keeps_trusted_path_without_wrapped_private_text(
    monkeypatch, tmp_path, error_type, chained, connect
):
    config_module.get_connection_config("gp")
    private_dsn = "postgresql://user:wrapped-private-secret@private-host/database"

    def original_failure():
        raise RuntimeError(private_dsn)

    def config_failure(_key):
        try:
            original_failure()
        except RuntimeError as cause:
            if chained:
                raise error_type(private_dsn) from cause
            raise error_type(private_dsn) from None

    monkeypatch.setattr(config_module, "get_connection_config", config_failure)
    result = sql.validate_connections(["missing"], connect=connect)[0]

    assert not result.valid
    assert result.connected is (False if connect else None)
    assert str(tmp_path / ".connections") in result.error
    assert error_type.__name__ in result.error
    assert "wrapped-private-secret" not in result.error
    assert private_dsn not in result.error


def test_validation_provenance_failure_does_not_replace_sanitized_configuration_error(monkeypatch):
    private_dsn = "postgresql://user:wrapped-private-secret@private-host/database"
    original = SqlConfigError(private_dsn)
    original.__cause__ = RuntimeError(private_dsn)

    def config_failure(_key):
        raise original

    def unavailable_source():
        raise RuntimeError(private_dsn)

    monkeypatch.setattr(config_module, "get_connection_config", config_failure)
    monkeypatch.setattr(validation_module, "_error_source_path", unavailable_source)
    result = sql.validate_connections(["missing"])[0]

    assert not result.valid
    assert result.connected is None
    assert result.error == "Configuration validation failed (SqlConfigError)."
