"""Lazy access to Airflow's task-time connection and variable APIs."""

from __future__ import annotations

from importlib import import_module
from typing import Any


def _airflow3() -> bool:
    airflow = import_module("airflow")
    major = str(getattr(airflow, "__version__", "2")).split(".", 1)[0]
    if major not in {"2", "3"}:
        message = f"Unsupported Airflow major version: {major}"
        raise ImportError(message)
    return major == "3"


def connection_getter() -> Any:
    if _airflow3():
        return import_module("airflow.sdk").Connection.get
    return import_module("airflow.hooks.base").BaseHook.get_connection


def variable_getter() -> Any:
    if _airflow3():
        return import_module("airflow.sdk").Variable.get
    return import_module("airflow.models.variable").Variable.get
