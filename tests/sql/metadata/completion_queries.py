from __future__ import annotations

from dataclasses import replace
from threading import Event
from types import SimpleNamespace

import pandas as pd
import pytest
from analytics_toolkit.sql.backends.metadata import (
    build_ch_completion_query,
    build_gp_completion_query,
    build_gp_show_tables_query,
    build_trino_completion_query,
)
from analytics_toolkit.sql_explorer import completion
from analytics_toolkit.sql_explorer.completion import CompletionCoordinator, CompletionRequest

from tests.sql.explorer.completion import FakeProvider


def test_names_only_queries_escape_prefixes_and_keep_partition_visibility() -> None:
    legacy = build_gp_completion_query("mart", "name_%", "legacy")
    modern = build_gp_completion_query("mart", "name_%", "declarative")
    ordinary = build_gp_completion_query(None, "", "none")
    assert "pg_partitions" in legacy
    assert "information_schema.tables parent" in legacy
    assert "child.relispartition" in modern
    assert "visible_parent" in modern
    assert "NOT EXISTS" not in ordinary
    for query in [
        legacy,
        modern,
        build_ch_completion_query("mart", "name_%"),
        build_trino_completion_query('"iceberg"', "mart", "name_%"),
    ]:
        assert "name!_!%%" in query
        assert "mart" in query
        assert "pg_total_relation_size" not in query
        assert "row_count" not in query
    detailed = build_gp_show_tables_query("mart", ["orders"], "row_count > 0")
    assert detailed.index("table_schema = 'mart'") < detailed.index("LEFT JOIN")
    assert "pg_total_relation_size" in detailed
    assert "row_count > 0" in detailed


def test_cache_prefix_expiry_and_repeated_metadata_requests() -> None:
    provider = FakeProvider()
    coordinator = CompletionCoordinator("gp", "gp", provider=provider)
    request = CompletionRequest("gp", "gp", "table", "sample")
    try:
        done = Event()
        coordinator.enqueue(request, on_success=lambda result: done.set())
        assert done.wait(2)
        assert coordinator.cached(replace(request, prefix="sample_o")) == ("sample_one",)
        assert coordinator.cached(replace(request, prefix="sam")) is None
        done.clear()
        coordinator.enqueue(request, on_success=lambda result: done.set())
        assert done.wait(2)
        assert len(provider.table_calls) == 1
        entry = coordinator._cache[request.cache_key]
        coordinator._cache[request.cache_key] = replace(entry, created_at=entry.created_at - 61)
        assert coordinator.cached(request) is None
        coordinator.invalidate_tables()
        assert coordinator.snapshot()[0] == 0
    finally:
        coordinator.stop()


def test_capability_probe_is_cached_and_missing_catalog_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queries = []

    def metadata(_key, query):
        queries.append(query)
        return pd.DataFrame({"value": ["legacy" if "partition_catalog" in query else "orders"]})

    monkeypatch.setattr(completion, "_metadata_frame", metadata)
    provider = completion.GreenplumCompletionProvider()
    assert provider.list_tables(connection_key="gp", prefix="o") == ("orders",)
    assert provider.list_tables(connection_key="gp", prefix="or") == ("orders",)
    assert len(queries) == 3
    monkeypatch.setattr(
        completion, "get_connection_config", lambda _key: SimpleNamespace(catalog=None)
    )
    with pytest.raises(ValueError, match="requires a catalog"):
        completion.TrinoCompletionProvider().list_tables(connection_key="trino", prefix="")
    assert "database =" not in build_ch_completion_query(None, "")
    assert "table_schema =" not in build_trino_completion_query("iceberg", None, "")
