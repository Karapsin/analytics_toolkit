from __future__ import annotations

from tests.sql._support.connection_config import (
    config_module,
    install_fake_airflow,
    pytest,
)


def test_unknown_airflow_connection_id_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_airflow(monkeypatch, {})

    with pytest.raises(
        config_module.UnsupportedConnectionTypeError,
        match="Unknown Airflow connection ID: missing",
    ):
        config_module.airflow_connection_config("missing", "gp")
