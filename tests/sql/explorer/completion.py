from __future__ import annotations

import asyncio
from dataclasses import replace
from threading import Event, Lock
from time import monotonic, sleep
from typing import TYPE_CHECKING

import pandas as pd
from analytics_toolkit.sql_explorer import completion as completion_module
from analytics_toolkit.sql_explorer.app import SqlEditor, SqlExplorerApp
from analytics_toolkit.sql_explorer.completion import (
    MIN_TABLE_PREFIX_LENGTH,
    ClickHouseCompletionProvider,
    CompletionCoordinator,
    CompletionRequest,
    GreenplumCompletionProvider,
    TrinoCompletionProvider,
    keyword_suggestions,
    normalize_completion_values,
    parse_completion_context,
    provider_for_backend,
)
from analytics_toolkit.sql_explorer.widgets import CompletionMenu
from textual.document._document import Selection

from tests.sql.explorer.app import FakeSession

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest


class FakeProvider:
    def __init__(self) -> None:
        self.table_calls: list[tuple[str, str | None, str | None]] = []
        self.calls: list[str] = []
        self.active = 0
        self.max_active = 0
        self.lock = Lock()

    def _enter(self, value: str) -> None:
        with self.lock:
            self.calls.append(value)
            self.active += 1
            self.max_active = max(self.max_active, self.active)

    def _leave(self) -> None:
        with self.lock:
            self.active -= 1

    def list_tables(
        self,
        *,
        connection_key: str,
        prefix: str,
        schema: str | None = None,
        catalog: str | None = None,
    ) -> tuple[str, ...]:
        assert connection_key
        self._enter(f"table:{prefix}")
        self.table_calls.append((prefix, schema, catalog))
        self._leave()
        return (f"{prefix}_one", f"{prefix}_two", "unrelated")

    def list_catalogs(self, *, connection_key: str) -> tuple[str, ...]:
        assert connection_key
        self._enter("catalog")
        sleep(0.01)
        self._leave()
        return ("iceberg", "hive")

    def list_schemas(
        self,
        *,
        connection_key: str,
        catalog: str | None = None,
    ) -> tuple[str, ...]:
        assert connection_key
        self._enter(f"schema:{catalog}")
        sleep(0.01)
        self._leave()
        return ("analytics", "sandbox")


class StubCoordinator:
    def __init__(self) -> None:
        self.catalogs: tuple[str, ...] | None = ()
        self.schemas: dict[str | None, tuple[str, ...] | None] = {None: ()}
        self.table_cache: tuple[str, ...] | None = None
        self.schema_enqueues = 0

    def stop(self) -> None:
        return None

    def remove_owner(self, owner_id: str) -> None:
        del owner_id

    def known_catalogs(self) -> tuple[str, ...] | None:
        return self.catalogs

    def cached_schemas(self, catalog: str | None = None) -> tuple[str, ...] | None:
        return self.schemas.get(catalog)

    def cached(self, _request: CompletionRequest) -> tuple[str, ...] | None:
        return self.table_cache

    def enqueue_schemas(self, **_kwargs: object) -> int:
        self.schema_enqueues += 1
        return self.schema_enqueues

    def enqueue(self, *_args: object, **_kwargs: object) -> int:
        return 1


def _install_stub(application: SqlExplorerApp) -> StubCoordinator:
    assert application._completion is not None
    application._completion.stop()
    stub = StubCoordinator()
    application._completion = stub  # type: ignore[assignment]
    return stub


def _wait_for(predicate: Callable[[], bool], *, timeout: float = 2.0) -> None:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if predicate():
            return
        sleep(0.01)
    message = "timed out waiting for metadata worker"
    raise AssertionError(message)


def test_local_keyword_context_and_suggestions_require_no_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sql_calls: list[str] = []
    monkeypatch.setattr(
        completion_module.sql,
        "show_tables",
        lambda *_args, **_kwargs: sql_calls.append("table"),
    )
    context = parse_completion_context("SEL", 3, backend="gp", connection_key="gp")
    assert context.request.kind == "keyword"
    assert keyword_suggestions(context.request.prefix) == ("select",)
    assert context.replacement_start == 0
    assert sql_calls == []


def test_completion_normalization_identity_and_empty_metadata_results() -> None:
    request = CompletionRequest("gp", "GP", "table", "Sample", context="from:0")
    assert request.identity == (*request.scope, "sample")
    assert normalize_completion_values(["", " Alpha ", "alpha", "Beta"]) == (
        "Alpha",
        "Beta",
    )
    assert completion_module._first_column_values(pd.DataFrame()) == ()


def test_table_contexts_and_qualified_prefix_replacement() -> None:
    for sql_text in (
        "SELECT * FROM samplet",
        "SELECT * FROM x JOIN samplet",
        "UPDATE samplet",
        "INSERT INTO samplet",
        "INTO samplet",
    ):
        context = parse_completion_context(
            sql_text,
            len(sql_text),
            backend="gp",
            connection_key="gp",
        )
        assert context.table_context is True
        assert context.request.prefix == "samplet"

    qualified = "SELECT * FROM analytics.samplet"
    context = parse_completion_context(
        qualified,
        len(qualified),
        backend="gp",
        connection_key="gp",
    )
    assert context.request.schema == "analytics"
    assert qualified[context.replacement_start : context.replacement_end] == "samplet"


def test_provider_routing_and_show_tables_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_show_tables(db_key: str, **kwargs: object) -> pd.DataFrame:
        calls.append((db_key, kwargs))
        return pd.DataFrame({"table_name": ["sampletable"]})

    monkeypatch.setattr(completion_module.sql, "show_tables", fake_show_tables)
    assert isinstance(provider_for_backend("gp"), GreenplumCompletionProvider)
    assert isinstance(provider_for_backend("clickhouse"), ClickHouseCompletionProvider)
    assert isinstance(provider_for_backend("trino"), TrinoCompletionProvider)

    GreenplumCompletionProvider().list_tables(
        connection_key="gp",
        prefix="sample",
        schema="analytics",
    )
    ClickHouseCompletionProvider().list_tables(
        connection_key="ch",
        prefix="sample",
        schema="events",
    )
    TrinoCompletionProvider().list_tables(
        connection_key="trino",
        prefix="sample",
        schema="sandbox",
        catalog="iceberg",
    )

    assert [call[0] for call in calls] == ["gp", "ch", "trino"]
    assert all("conditions" in options for _, options in calls)
    assert "sample%" in str(calls[0][1]["conditions"])
    assert calls[2][1]["trino_catalog"] == "iceberg"
    assert all("table_name" not in options for _, options in calls)


def test_coordinator_uses_one_lookup_and_filters_extensions_locally() -> None:
    provider = FakeProvider()
    coordinator = CompletionCoordinator("gp", "gp", provider=provider)
    completed = Event()
    request = CompletionRequest(
        "gp",
        "gp",
        "table",
        "sample",
        schema="analytics",
        context="from:0",
    )
    coordinator.enqueue(request, on_success=lambda _result: completed.set())
    assert completed.wait(2)

    longer = replace(request, prefix="sample_o")
    shorter = replace(request, prefix="sam")
    assert coordinator.cached(longer) == ("sample_one",)
    assert coordinator.cached(shorter) == ("sample_one", "sample_two")
    coordinator.enqueue(longer)
    coordinator.enqueue(shorter)
    assert provider.table_calls == [("sample", "analytics", None)]

    changed = replace(request, schema="other")
    changed_done = Event()
    coordinator.enqueue(changed, on_success=lambda _result: changed_done.set())
    assert changed_done.wait(2)
    assert len(provider.table_calls) == 2
    coordinator.stop()


def test_duplicate_requests_are_skipped_while_one_request_is_in_flight() -> None:
    release = Event()
    started = Event()

    class SlowProvider(FakeProvider):
        def list_tables(self, **kwargs: object) -> tuple[str, ...]:
            started.set()
            release.wait(2)
            return super().list_tables(**kwargs)

    provider = SlowProvider()
    coordinator = CompletionCoordinator("gp", "gp", provider=provider)
    request = CompletionRequest("gp", "gp", "table", "sample", context="from:0")
    coordinator.enqueue(request)
    assert started.wait(2)
    coordinator.enqueue(replace(request, prefix="sample_more"))
    assert coordinator.snapshot()[2] is True
    release.set()
    _wait_for(lambda: coordinator.snapshot()[2] is False)
    assert len(provider.table_calls) == 1
    coordinator.stop()


def test_trino_bootstrap_runs_catalog_then_each_schema_serially() -> None:
    provider = FakeProvider()
    coordinator = CompletionCoordinator("trino", "trino", provider=provider)
    coordinator.start_bootstrap()
    _wait_for(lambda: len(provider.calls) == 3 and coordinator.snapshot()[2] is False)

    assert provider.calls[0] == "catalog"
    assert set(provider.calls[1:]) == {"schema:iceberg", "schema:hive"}
    assert provider.max_active == 1
    assert coordinator.known_catalogs() == ("hive", "iceberg")
    assert coordinator.cached_schemas("iceberg") == ("analytics", "sandbox")
    coordinator.stop()


def test_conditional_tab_completion_navigation_acceptance_and_escape() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test() as pilot:
            editor = application.query_one(SqlEditor)
            editor.text = "SEL"
            editor.cursor_location = (0, 3)

            await pilot.press("ctrl+space")
            menu = application.query_one(CompletionMenu)
            assert menu.is_open is False
            assert editor.text == "select"
            assert application.focused is editor

            editor.text = "L"
            editor.cursor_location = (0, 1)
            await pilot.press("ctrl+space")
            assert menu.is_open is True
            assert menu.suggestions == ("left join", "limit")
            assert application.focused is editor
            await pilot.press("i")
            assert editor.text == "Li"
            assert menu.suggestions == ("limit",)
            await pilot.press("backspace")
            assert editor.text == "L"
            assert menu.suggestions == ("left join", "limit")
            await pilot.press("down", "enter")
            assert editor.text == "limit"

            editor.text = "L"
            editor.cursor_location = (0, 1)
            await pilot.press("ctrl+space")
            await pilot.press("escape")
            assert menu.is_open is False
            assert application.focused is editor

            editor.text = "select 1"
            editor.cursor_location = (0, 0)
            await pilot.press("ctrl+space")
            assert editor.text == "select 1"

    asyncio.run(exercise())


def test_catalog_database_schema_and_clause_changes_create_new_scopes() -> None:
    provider = FakeProvider()
    coordinator = CompletionCoordinator("trino", "trino", provider=provider)
    base = CompletionRequest(
        "trino",
        "trino",
        "table",
        "sample",
        schema="analytics",
        catalog="iceberg",
        database="lake",
        context="from:0",
    )
    requests = (
        base,
        replace(base, catalog="hive"),
        replace(base, database="warehouse"),
        replace(base, schema="sandbox"),
        replace(base, context="join:30"),
    )
    for expected_count, request in enumerate(requests, 1):
        completed = Event()
        coordinator.enqueue(
            request,
            on_success=lambda _result, completed=completed: completed.set(),
        )
        assert completed.wait(2)
        assert len(provider.table_calls) == expected_count
    coordinator.stop()


def test_backend_namespace_discovery_uses_serial_metadata_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, dict[str, object]]] = []

    def fake_read(db_key: str, query: str, **kwargs: object) -> pd.DataFrame:
        calls.append((db_key, query, kwargs))
        if query == "SHOW CATALOGS":
            return pd.DataFrame({"catalog": ["hive"]})
        if query.startswith("SHOW SCHEMAS"):
            return pd.DataFrame({"schema": ["sandbox"]})
        if query == "SHOW DATABASES":
            return pd.DataFrame({"database": ["default"]})
        return pd.DataFrame({"schema_name": ["public"]})

    monkeypatch.setattr(completion_module.sql, "read", fake_read)
    assert GreenplumCompletionProvider().list_schemas(connection_key="gp") == ("public",)
    assert ClickHouseCompletionProvider().list_schemas(connection_key="ch") == ("default",)
    trino = TrinoCompletionProvider()
    assert GreenplumCompletionProvider().list_catalogs(connection_key="gp") == ()
    assert ClickHouseCompletionProvider().list_catalogs(connection_key="ch") == ()
    assert trino.list_catalogs(connection_key="trino") == ("hive",)
    assert trino.list_schemas(connection_key="trino") == ("sandbox",)
    assert trino.list_schemas(connection_key="trino", catalog="hive") == ("sandbox",)
    assert all(call[2]["query_label"].startswith("sql_explorer metadata=") for call in calls)
    assert 'FROM "hive"' in calls[-1][1]


def test_coordinator_keyword_and_idle_worker_paths() -> None:
    coordinator = CompletionCoordinator("gp", "gp", provider=FakeProvider())
    completed: list[completion_module.CompletionResult] = []
    request = CompletionRequest("gp", "gp", "keyword", "SEL", context="keyword:0")
    request_id = coordinator.enqueue(request, on_success=completed.append)
    assert completed == [completion_module.CompletionResult(request, request_id, ("select",))]
    assert coordinator._run(request) == ("select",)

    coordinator._wake.set()
    sleep(0.05)
    assert coordinator.snapshot() == (0, 0, False)
    coordinator.stop()


def test_backend_qualified_completion_context_variants() -> None:
    trino_three = parse_completion_context(
        "SELECT * FROM iceberg.analytics.sample",
        len("SELECT * FROM iceberg.analytics.sample"),
        backend="trino",
    )
    assert (trino_three.request.catalog, trino_three.request.schema) == (
        "iceberg",
        "analytics",
    )

    trino_catalog = parse_completion_context(
        "SELECT * FROM iceberg.sample",
        len("SELECT * FROM iceberg.sample"),
        backend="trino",
        trino_catalogs=("iceberg",),
    )
    assert trino_catalog.request.catalog == "iceberg"
    assert trino_catalog.request.schema is None

    trino_schema = parse_completion_context(
        "SELECT * FROM analytics.sample",
        len("SELECT * FROM analytics.sample"),
        backend="trino",
        trino_catalogs=("iceberg",),
    )
    assert trino_schema.request.catalog is None
    assert trino_schema.request.schema == "analytics"

    clickhouse = parse_completion_context(
        "SELECT * FROM events.sample",
        len("SELECT * FROM events.sample"),
        backend="clickhouse",
    )
    assert clickhouse.request.schema == "events"
    assert clickhouse.request.database == "events"


def test_app_table_lookup_uses_initial_six_chars_and_rejects_stale_result() -> None:
    provider = FakeProvider()

    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test() as pilot:
            assert MIN_TABLE_PREFIX_LENGTH == 6
            assert application._completion is not None
            application._completion.stop()
            application._completion = CompletionCoordinator("gp", "gp", provider=provider)
            editor = application.query_one(SqlEditor)
            editor.text = "SELECT * FROM sampletable"
            editor.cursor_location = (0, len(editor.text))

            await pilot.press("ctrl+space")
            _wait_for(lambda: len(provider.table_calls) == 1)
            await pilot.pause()
            assert provider.table_calls[0][0] == "sample"

            application.query_one(CompletionMenu).action_close()
            editor.text = "SELECT * FROM other_context"
            editor.cursor_location = (0, len(editor.text))
            stale = CompletionRequest("gp", "gp", "table", "sample", context="from:0")
            application._receive_completion(
                completion_module.CompletionResult(stale, 1, ("sample_one",))
            )
            assert application.query_one(CompletionMenu).is_open is False

    asyncio.run(exercise())


def test_cursor_change_cancels_inflight_and_fresh_cache_handles_backspace() -> None:
    started = Event()
    release = Event()

    class SlowProvider(FakeProvider):
        def list_tables(self, **kwargs: object) -> tuple[str, ...]:
            started.set()
            release.wait(2)
            return super().list_tables(**kwargs)

    provider = SlowProvider()

    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test() as pilot:
            assert application._completion is not None
            application._completion.stop()
            application._completion = CompletionCoordinator("gp", "gp", provider=provider)
            editor = application.query_one(SqlEditor)
            editor.text = "SELECT * FROM sample"
            editor.cursor_location = (0, len(editor.text))
            await pilot.press("ctrl+space")
            assert started.wait(2)

            cursor = editor.cursor_location
            edit = editor.replace("_o", cursor, cursor, maintain_selection_offset=False)
            editor.cursor_location = edit.end_location
            await pilot.pause()
            release.set()
            _wait_for(lambda: len(provider.table_calls) == 1)
            await pilot.pause()
            menu = application.query_one(CompletionMenu)
            assert menu.is_open is False
            assert editor.text == "SELECT * FROM sample_o"
            await pilot.press("ctrl+space")
            await pilot.pause()
            assert editor.text == "SELECT * FROM sample_one"

            menu.action_close()
            editor.text = "SELECT * FROM sampl"
            editor.cursor_location = (0, len(editor.text))
            await pilot.press("ctrl+space")
            assert set(menu.suggestions) == {"sample_one", "sample_two"}
            assert len(provider.table_calls) == 2

            menu.action_close()
            editor.text = "SELECT * FROM sample JOIN sample"
            editor.cursor_location = (0, len(editor.text))
            await pilot.press("ctrl+space")
            _wait_for(lambda: len(provider.table_calls) == 3)

    asyncio.run(exercise())


def test_metadata_failure_does_not_remove_local_keyword_completion() -> None:
    class BrokenProvider(FakeProvider):
        def list_tables(self, **_kwargs: object) -> tuple[str, ...]:
            message = "metadata unavailable"
            raise RuntimeError(message)

    errors: list[str] = []
    coordinator = CompletionCoordinator("gp", "gp", provider=BrokenProvider())
    request = CompletionRequest("gp", "gp", "table", "sample", context="from:0")
    coordinator.enqueue(request, on_error=lambda _result, exc: errors.append(str(exc)))
    _wait_for(lambda: bool(errors))
    assert errors == ["metadata unavailable"]
    assert keyword_suggestions("SEL") == ("select",)
    coordinator.stop()


def test_app_completion_request_defensive_paths() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test() as pilot:
            stub = _install_stub(application)
            editor = application.query_one(SqlEditor)
            menu = application.query_one(CompletionMenu)

            editor.text = "SEL"
            editor.cursor_location = (0, 3)
            await pilot.press("ctrl+space")
            assert menu.is_open is False
            assert editor.text == "select"
            await pilot.pause()

            editor.text = "L"
            editor.cursor_location = (0, 1)
            editor.action_completion_or_indent()
            assert menu.suggestions == ("left join", "limit")
            menu.action_accept()
            assert editor.text == "left join"

            editor.text = "SELECT * FROM sample"
            editor.selection = Selection((0, 14), (0, 16))
            assert application._request_completion() is False
            editor.selection = Selection.cursor((0, len(editor.text)))

            application._completion = None
            assert application._request_completion() is False
            context = application._completion_at_cursor()
            assert application._open_namespace_completion(context) is False
            application._completion = stub  # type: ignore[assignment]

            editor.text = "SELECT * FROM sam"
            editor.cursor_location = (0, len(editor.text))
            assert application._request_completion() is False
            assert "at least 6" in str(application.query_one("#notice").render())

            stub.schemas[None] = ("sample_schema",)
            assert application._request_completion() is True
            assert editor.text == "SELECT * FROM sample_schema"
            assert menu.is_open is False

            editor.text = "SELECT * FROM sample"
            editor.cursor_location = (0, len(editor.text))
            stub.schemas[None] = ()
            stub.table_cache = ()
            assert application._request_completion() is True
            assert menu.is_open is False

    asyncio.run(exercise())


def test_app_completion_namespace_paths() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test():
            stub = _install_stub(application)
            editor = application.query_one(SqlEditor)
            menu = application.query_one(CompletionMenu)
            editor.text = "SELECT * FROM sample"
            editor.cursor_location = (0, len(editor.text))

            stub.table_cache = None
            stub.schemas[None] = None
            gp_context = application._completion_at_cursor()
            assert application._open_namespace_completion(gp_context) is True
            assert stub.schema_enqueues == 1

            qualified = parse_completion_context(
                "SELECT * FROM analytics.sample",
                len("SELECT * FROM analytics.sample"),
                backend="gp",
                connection_key="gp",
            )
            assert application._open_namespace_completion(qualified) is True

            stub.catalogs = ("iceberg",)
            trino_catalog = parse_completion_context(
                "SELECT * FROM ice",
                len("SELECT * FROM ice"),
                backend="trino",
                connection_key="trino",
            )
            assert application._open_namespace_completion(trino_catalog) is True
            menu.action_close()

            stub.schemas["iceberg"] = ("sample",)
            trino_schema = parse_completion_context(
                "SELECT * FROM iceberg.sam",
                len("SELECT * FROM iceberg.sam"),
                backend="trino",
                connection_key="trino",
                trino_catalogs=("iceberg",),
            )
            assert application._open_namespace_completion(trino_schema) is True
            menu.action_close()

            application._receive_namespace(
                completion_module.CompletionResult(gp_context.request, 1, ())
            )
            editor.text = "SEL"
            editor.cursor_location = (0, 3)
            application._receive_namespace(
                completion_module.CompletionResult(
                    CompletionRequest("gp", "gp", "keyword", "S", context="keyword:0"),
                    2,
                    (),
                )
            )

    asyncio.run(exercise())


def test_app_completion_empty_acceptance_and_refresh_paths() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test():
            stub = _install_stub(application)
            editor = application.query_one(SqlEditor)
            menu = application.query_one(CompletionMenu)

            menu.open(())
            application._accept_completion()
            application._refresh_open_completion()

            application._completion_context = None
            menu.open(("sample",))
            application._refresh_open_completion()
            assert menu.is_open is False

            editor.text = "SELECT * FROM sample"
            editor.cursor_location = (0, len(editor.text))
            current = application._completion_at_cursor()
            application._completion_context = current
            stub.schemas[None] = ()
            stub.table_cache = None
            menu.open(("sample",))
            application._refresh_open_completion()
            assert menu.is_open is True

            stub.table_cache = ()
            application._refresh_open_completion()
            assert menu.is_open is False

    asyncio.run(exercise())
