"""Installed-extra smoke and focused compatibility checks, without external services."""

from __future__ import annotations

import argparse
import importlib
import subprocess
import sys
from unittest.mock import patch

from .python_support import EXTRAS

MODULES = {
    "airflow": ("airflow",),
    "clickhouse-native": ("clickhouse_driver",),
    "tui": ("textual", "pyperclip", "analytics_toolkit.sql_explorer.app"),
    "datalens": ("datalens_sdk",),
}
SUITES = {
    "airflow": ("tests/sql/connection/airflow.py", "tests/sql/connection/airflow_compat.py"),
    "clickhouse-native": ("tests/sql/connection/config/clickhouse.py",),
    "tui": ("tests/sql/explorer/launcher.py",),
    "datalens": ("tests/datalens_utils",),
}


def airflow_smoke() -> None:
    """Exercise the actual installed API classes at a mocked lookup boundary."""
    from analytics_toolkit.sql.connection import (  # noqa: PLC0415 - optional import
        airflow_compat,
        config,
    )
    from analytics_toolkit.sql.connection.references import _read_airflow_variable  # noqa: PLC0415

    airflow = importlib.import_module("airflow")
    if airflow.__version__.startswith("3."):
        connection_class = importlib.import_module("airflow.sdk").Connection
        connection_path = "airflow.sdk.Connection.get"
        variable_path = "airflow.sdk.Variable.get"
    else:
        models = importlib.import_module("airflow.models")
        models.import_all_models()
        connection_class = models.Connection
        connection_path = "airflow.hooks.base.BaseHook.get_connection"
        variable_path = "airflow.models.variable.Variable.get"
    connection = connection_class(
        conn_id="atk_smoke",
        conn_type="postgres",
        host="localhost",
        login="user",
        password="dummy",  # noqa: S106 - isolated smoke data, no network access
        schema="db",
    )
    connection_patch = patch(connection_path, return_value=connection)
    variable_patch = patch(variable_path, return_value="value")
    with connection_patch, variable_patch:
        resolved = config.airflow_connection_config("atk_smoke", "gp")
        if (
            resolved.host != "localhost"
            or _read_airflow_variable("atk_smoke", "host", "var") != "value"
        ):
            message = "Installed Airflow API failed the toolkit compatibility smoke check"
            raise RuntimeError(message)
        if not callable(airflow_compat.variable_getter()):
            message = "Airflow Variable API is unavailable"
            raise TypeError(message)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("extra", choices=[*EXTRAS, "all"])
    args = parser.parse_args()
    extras = EXTRAS if args.extra == "all" else (args.extra,)
    suites = []
    for extra in extras:
        for module in MODULES[extra]:
            importlib.import_module(module)
        if extra == "airflow":
            airflow_smoke()
        suites.extend(SUITES[extra])
    subprocess.run([sys.executable, "-m", "pytest", "-q", *suites], check=True)


if __name__ == "__main__":
    main()
