"""Actual Trino formatters, including the minimum supported SDK's original code."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from functools import lru_cache
from importlib.machinery import SourceFileLoader

from trino.dbapi import Cursor

from tests._support.paths import REPO_ROOT
from tests.sql._support import trino_parameters
from tests.sql._support.trino_time_values import native_time_target


@lru_cache(maxsize=1)
def _minimum_cursor():
    directory = REPO_ROOT / "tests/sql/_support/data"
    source = directory / "trino_0_320_dbapi.py.txt"
    provenance = json.loads((directory / "trino_0_320_provenance.json").read_text())
    assert hashlib.sha256(source.read_bytes()).hexdigest() == provenance["source_sha256"]
    license_data = (directory / "trino_0_320_LICENSE.txt").read_bytes()
    assert hashlib.sha256(license_data).hexdigest() == provenance["license_sha256"]
    loader = SourceFileLoader("_atk_test_original_trino_0320_dbapi", str(source))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module.Cursor


def use_actual_formatter(monkeypatch, sdk):
    """Run the real method body, without claiming an old client transport stack."""
    formatter = _minimum_cursor() if sdk == "minimum-0.320" else Cursor
    monkeypatch.setattr(trino_parameters, "Cursor", formatter)


def binary_target(
    monkeypatch, native_type, expected_wire, expected_value, *, original_value=b"original"
):
    database = native_time_target(monkeypatch, native_type)
    database.table_rows[database.target] = [original_value]

    def decode(wire):
        if wire == "NULL":
            return None
        assert wire == expected_wire
        return expected_value

    monkeypatch.setattr(database, "decode_cast", decode)
    return database
