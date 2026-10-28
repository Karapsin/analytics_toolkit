from __future__ import annotations

# ruff: noqa: E501
import importlib
import json
from pathlib import Path
from typing import TYPE_CHECKING

import analytics_toolkit.general as general_module
import pytest

from tests._support import extras as _extras

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

# Lagging extras omitted from a compatibility environment (docs/PYTHON_SUPPORT.md).
# A hook rather than ``collect_ignore``: nested conftests define their own lists.
_EXTRA_IGNORED = frozenset(
    Path(__file__).parent / path for path in _extras.collect_ignore(Path(__file__).parent)
)


def pytest_ignore_collect(collection_path: Path) -> bool | None:
    if collection_path in _EXTRA_IGNORED or _EXTRA_IGNORED.intersection(collection_path.parents):
        return True
    return None


@pytest.fixture(autouse=True)
def deterministic_terminal_colors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep headless color assertions independent of the host's terminal preference."""
    # Textual reads this when constructing each App and filters composed strips.
    # Tests of monochrome behavior opt in explicitly after this fixture runs.
    monkeypatch.delenv("NO_COLOR", raising=False)


DEFAULT_SQL_CONNECTIONS = {
    "gp": {
        "type": "gp",
        "host": "gp.example",
        "port": 5432,
        "user": "user",
        "password": "password",
        "database": "db",
    },
    "gp_sandbox": {
        "type": "gp",
        "host": "gp-sandbox.example",
        "port": 5432,
        "user": "user",
        "password": "password",
        "database": "sandbox",
    },
    "trino": {
        "type": "trino",
        "host": "trino.example",
        "port": 8080,
        "user": "user",
        "password": "password",
        "catalog": "iceberg",
        "schema": "sandbox",
        "upsert_partition_drop_sql_template": (
            "ALTER TABLE {table} DROP PARTITION ({partition_column} = {partition_value})"
        ),
    },
    "ch": {
        "type": "ch",
        "host": "ch.example",
        "port": 8123,
        "user": "user",
        "password": "password",
        "database": "default",
        "transfer_staging_schema": "analytics_toolkit_transfer",
        "ddl_defaults": {
            "regular": {
                "create_distributed_pair": True,
                "shard": {"engine": "ReplicatedMergeTree", "on_cluster": "{cluster}"},
                "distributed": {
                    "engine_template": "Distributed({cluster}, {database}, {shard_table}, {sharding_key})",
                    "cluster": "{cluster}",
                    "on_cluster": "{cluster}",
                    "sharding_key": "rand()",
                },
            },
            "staging": {
                "create_distributed_pair": False,
                "shard": {"engine": "MergeTree", "on_cluster": None},
            },
        },
    },
}


@pytest.fixture
def write_sql_connections(tmp_path: Path) -> Callable[[dict[str, dict[str, object]]], Path]:
    def write(connections: dict[str, dict[str, object]]) -> Path:
        connections_file = tmp_path / ".connections"
        connections_file.write_text(
            json.dumps(connections, indent=2),
            encoding="utf-8",
        )
        return connections_file

    return write


@pytest.fixture(autouse=True)
def default_sql_connections(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> Iterator[None]:
    config_path_module = importlib.import_module("analytics_toolkit.sql.connection.config_path")
    monkeypatch.setattr(config_path_module, "_resolve_calling_base_dir", lambda: None)
    general_module.set_connections_path(None)
    monkeypatch.chdir(tmp_path)
    write_sql_connections(DEFAULT_SQL_CONNECTIONS)
    yield
    general_module.set_connections_path(None)
