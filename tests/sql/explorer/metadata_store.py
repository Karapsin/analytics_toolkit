from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from typing import TYPE_CHECKING

import pytest
from analytics_toolkit.sql_explorer.metadata_store import MetadataStore

if TYPE_CHECKING:
    from pathlib import Path


def test_concurrent_schema_writes_preserve_other_snapshots(tmp_path: Path) -> None:
    first = MetadataStore(tmp_path, "connection-a")
    second = MetadataStore(tmp_path, "connection-b")
    first.save({})

    def update(index: int) -> None:
        store = first if index % 2 else second
        store.update(("table", "", str(index)), (f"table_{index}",))

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(update, range(20)))
    for index in range(20):
        snapshot = (first if index % 2 else second).load()
        assert snapshot[("table", "", str(index))] == (f"table_{index}",)
    first.save({})
    assert len(second.load()) == 10


@pytest.mark.parametrize("payload", ["[1]", "{}", '"names"'])
def test_invalid_snapshot_payload_is_rejected(tmp_path: Path, payload: str) -> None:
    store = MetadataStore(tmp_path, "connection")
    store.save({("table", "", "public"): ("orders",)})
    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.execute("UPDATE snapshots SET names = ?", (payload,))
    with pytest.raises(ValueError, match="Invalid cached metadata"):
        store.load()
