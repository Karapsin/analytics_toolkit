from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pandas as pd
import pytest
from analytics_toolkit.sql.backends.ch.native_client import NativeClickHouseClient
from analytics_toolkit.sql.backends.ch.routing import ChClusterRouting, wrap_client
from analytics_toolkit.sql.backends.row_limits import apply_row_limit
from analytics_toolkit.sql.execution.cancellation import AsyncSqlCancelled, SqlCancellationScope
from analytics_toolkit.sql.execution.observation import observe_sql, observed_connection
from analytics_toolkit.sql_explorer.journal import JournalAction, QueryJournal, safe_component
from analytics_toolkit.sql_explorer.journal_metadata import describe_sql
from analytics_toolkit.sql_explorer.journal_reader import read_page, read_record
from analytics_toolkit.sql_explorer.journal_workers import JournalMetadataProvider, cancel_metadata
from analytics_toolkit.sql_explorer.statements import build_execution_plan

from tests.sql.explorer.runtime import _session

if TYPE_CHECKING:
    from pathlib import Path


def records(journal: QueryJournal, alias: str, **kwargs: Any) -> list[dict[str, Any]]:
    return [
        read_record(journal, alias, r["action_id"])
        for r in read_page(journal, alias, **kwargs).records
    ]


class Driver:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.autocommit = False

    def cursor(self) -> Driver:
        return self

    def execute(self, sql: str) -> Driver:
        self.calls.append(sql)
        if sql == "fail":
            message = "driver failure"
            raise ValueError(message)
        return self

    def query(self, *, query: str) -> str:
        self.calls.append(query)
        return "result"

    def command(self, sql: str) -> str:
        self.calls.append(sql)
        return "ok"

    def raw_query(self, sql: str) -> bytes:
        self.calls.append(sql)
        return b"ok"

    def __enter__(self) -> Driver:  # noqa: PYI034 -- Python 3.8 test fixture.
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def __iter__(self) -> Any:
        return iter([1])


def test_journal_preserves_user_sql_and_records_all_submissions(tmp_path: Path) -> None:
    journal = QueryJournal(tmp_path)
    original = "-- my query\r\nselect * from lake.sales.orders;\n"
    driver = Driver()
    assert observed_connection(driver) is driver
    with journal.action("lake", "trino", "user", user_sql=original, source_file="/sql/report.sql"):
        connection = observed_connection(driver)
        connection.autocommit = True
        with connection.cursor() as cursor:
            assert cursor.execute("SELECT * FROM (select 1) q LIMIT 201") is cursor
            assert list(cursor) == [1]
        assert connection.query(query="SHOW CATALOGS") == "result"
        assert connection.command("SET something") == "ok"
        assert connection.raw_query("SELECT 2") == b"ok"
    record = records(journal, "lake")[0]
    assert record["outcome"] == "completed"
    assert len(record["submissions"]) == 4
    assert record["submissions"][0]["outcome"] == "completed"
    assert driver.autocommit
    assert observed_connection(driver) is driver
    directory = journal.alias_directory("lake")
    assert record["user_sql"] == original
    assert record["source_file"] == "/sql/report.sql"
    assert not list(directory.rglob("*.sql"))
    assert not list(directory.rglob("*.json"))
    assert record["statements"][0]["objects"] == [
        {"kind": "table", "catalog": "lake", "schema": "sales", "name": "orders"}
    ]
    assert record["elapsed_seconds"] >= 0
    assert list(directory.rglob("*.tmp")) == []


def test_failures_cancellations_and_unfinished_records(tmp_path: Path) -> None:
    journal = QueryJournal(tmp_path)
    with pytest.raises(ValueError, match="driver failure"), journal.action("gp", "gp", "user"):
        observed_connection(Driver()).execute("fail")
    failed = records(journal, "gp")[0]
    assert failed["outcome"] == "failed"
    assert failed["submissions"][0]["error"]["type"] == "ValueError"
    with pytest.raises(AsyncSqlCancelled), journal.action("gp", "gp", "user"):
        raise AsyncSqlCancelled
    assert records(journal, "gp")[0]["outcome"] == "cancelled"
    action = JournalAction(journal, "gp", "gp", "user", "select 1", None, None)
    action.start()
    assert records(journal, "gp")[0]["outcome"] == "running"
    assert records(journal, "gp")[0]["finished_at"] is None


def test_concurrent_writers_aliases_and_read_pages(tmp_path: Path) -> None:
    journal = QueryJournal(tmp_path)

    def write(index: int) -> None:
        with journal.action("gp", "gp", "user", user_sql=f"select {index}"):
            observed_connection(Driver()).execute(f"select {index}")

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(write, range(8)))
    with journal.action("other", "gp", "user", user_sql="select 999"):
        pass
    first = read_page(journal, "gp", limit=3)
    assert len(first.records) == 3
    second = read_page(journal, "gp", limit=5, before=first.cursor)
    assert len(second.records) == 5
    assert not {r["action_id"] for r in first.records} & {r["action_id"] for r in second.records}
    assert len(records(journal, "gp", query="select 7")) == 1
    assert records(journal, "other")[0]["user_sql"] == "select 999"
    assert len(records(journal, "gp")) == 8
    assert records(journal, "missing") == []


def test_internal_metadata_is_json_only(tmp_path: Path) -> None:
    journal = QueryJournal(tmp_path)

    class Provider:
        def list_tables(self, **kwargs: Any) -> tuple[str, ...]:
            observed_connection(Driver()).execute(
                "select table_name from information_schema.tables"
            )
            return ("orders",)

        list_catalogs = list_tables
        list_schemas = list_tables

    provider = JournalMetadataProvider(Provider(), journal, "trino", "background")
    assert provider.list_tables(
        connection_key="lake", prefix="", catalog="ice", schema="sales"
    ) == ("orders",)
    provider.list_catalogs(connection_key="lake")
    provider.list_schemas(connection_key="lake", catalog="ice")
    assert not records(journal, "lake")
    entries = records(journal, "lake", include_internal=True)
    assert len(entries) == 3
    assert entries[-1]["context"]["schema"] == "sales"
    assert entries[-1]["submissions"][0]["statements"][0]["objects"][0]["name"] == "tables"
    assert not list(journal.alias_directory("lake").glob("queries/*.sql"))


def test_bad_storage_warns_without_changing_sql_error(tmp_path: Path) -> None:
    directory = tmp_path / "file"
    directory.write_text("not a directory")
    journal = QueryJournal(directory)
    with pytest.raises(ValueError, match="driver failure"), journal.action(
        "gp", "gp", "user", user_sql="fail"
    ):
        observed_connection(Driver()).execute("fail")
    assert "Query journal unavailable" in (journal.take_warning() or "")
    assert journal.take_warning() is None
    journal.warn(OSError("again"))
    assert journal.take_warning() is None


def test_malformed_database_is_reported_without_replacing_it(tmp_path: Path) -> None:
    journal = QueryJournal(tmp_path)
    path = journal.store("gp").path
    path.parent.mkdir(parents=True)
    path.write_bytes(b"broken SQLite database")
    page = read_page(journal, "gp")
    assert page.records == ()
    assert "Cannot read journal" in page.warning
    with journal.action("gp", "gp", "user", user_sql="select 1"):
        pass
    assert path.read_bytes() == b"broken SQLite database"
    assert journal.take_warning()


@pytest.mark.parametrize("alias", ["../gp", "a/b", "a\\b", "con", "漢字", ".", "a" * 200])
def test_safe_aliases(alias: str, tmp_path: Path) -> None:
    journal = QueryJournal(tmp_path)
    assert journal.alias_directory(alias).parent == journal.directory
    assert safe_component(alias) != safe_component(alias + "_")
    assert len(safe_component(alias)) < 100


def test_alias_folders_remain_distinct_on_case_insensitive_filesystems(tmp_path: Path) -> None:
    journal = QueryJournal(tmp_path)
    aliases = ["lake", "Lake", "LAKE", "a/b", "a_b", safe_component("a/b"), ""]
    folders = {journal.alias_directory(alias).name.casefold() for alias in aliases}
    assert len(folders) == len(aliases)


@pytest.mark.parametrize("backend", ["gp", "trino", "ch"])
def test_metadata_excludes_cte_aliases_and_retains_qualified_names(backend: str) -> None:
    items = describe_sql(
        'WITH recent AS (SELECT * FROM "Sales".orders) SELECT * FROM recent r '
        "JOIN catalog.public.users u ON r.id=u.id",
        backend,
    )
    assert items[0]["action"] == "select"
    assert {o["name"] for o in items[0]["objects"]} == {"orders", "users"}
    assert any(o["schema"] == "Sales" for o in items[0]["objects"])
    assert (
        describe_sql("CREATE TABLE public.copy AS SELECT * FROM public.original", backend)[0][
            "action"
        ]
        == "create"
    )
    assert (
        describe_sql("INSERT INTO public.copy SELECT * FROM public.original", backend)[0]["action"]
        == "insert"
    )
    assert describe_sql("this is ??? unsupported", backend)[0]["parsed"] is False


def test_runtime_journal_keeps_original_and_export_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session = _session(monkeypatch, tmp_path)
    session.journal = QueryJournal(tmp_path)

    def read(alias: str, query: str, **kwargs: Any) -> pd.DataFrame:
        query, _ = apply_row_limit(query, kwargs.get("row_limit"), dialect="postgres")
        observed_connection(Driver()).execute(query)
        return pd.DataFrame({"value": range(202)})

    monkeypatch.setattr("analytics_toolkit.sql_explorer.runtime.sql.read", read)
    source = "-- selected fragment\nselect value from public.orders;"
    plan = replace(build_execution_plan(source, "gp"), source_file="/sql/orders.sql")
    session.execute(plan)
    record = records(session.journal, "gp")[0]
    assert record["user_sql"] == source
    assert "LIMIT 201" in record["submissions"][0]["sql"]
    session.database = type(session.database)("other", "gp")
    session.export_dataframe()
    export = records(session.journal, "gp")[0]
    assert export["origin"] == "export"
    assert export["user_sql"] == source
    assert "LIMIT 201" not in export["submissions"][0]["sql"]
    assert not records(session.journal, "other")


def test_persist_errors_do_not_escape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    journal = QueryJournal(tmp_path)
    with journal.action("gp", "gp", "user", user_sql="select 1") as action:
        action.record["error"] = object()
        action._save(action.store.finish_action, action.record)
    assert journal.take_warning() is not None
    # A fresh observer context always restores its parent, even on errors.
    with journal.action("gp", "gp", "user") as outer:
        with journal.action("gp", "gp", "user"):
            observed_connection(Driver()).execute("inner")
        observed_connection(Driver()).execute("outer")
    assert [
        s["sql"] for s in read_record(journal, "gp", outer.record["action_id"])["submissions"]
    ] == ["outer"]


def test_statement_metadata_namespace_ddl_and_deduplicated_tables() -> None:
    for backend in ("gp", "trino", "ch"):
        schema = describe_sql("CREATE SCHEMA catalog.sales", backend)[0]
        assert schema["objects"] == [
            {"kind": "schema", "catalog": "catalog", "schema": None, "name": "sales"}
        ]
        refs = describe_sql(
            "select * from sales.orders a join sales.orders b on a.id=b.id", backend
        )[0]
        assert len(refs["objects"]) == 1
    driver = Driver()
    # Delegation of non-SQL attributes is required by DB-API consumers.
    with observe_sql(SimpleNamespace(submission=lambda sql: nullcontext())):
        assert observed_connection(driver).autocommit is False


def test_export_failure_cancellation_and_idle_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(monkeypatch, tmp_path)
    session.journal = QueryJournal(tmp_path)
    assert session.cancel_active().matched_queries == 0
    for cancelled in (False, True):
        monkeypatch.setattr(
            "analytics_toolkit.sql_explorer.runtime.sql.read",
            lambda *a, **kw: pd.DataFrame({"v": range(201)}),
        )
        session.execute(session.plan("select * from sales.orders"))

        def fail(*args: Any, cancelled: bool = cancelled, **kwargs: Any) -> None:
            if cancelled:
                session._cancellation_requested_for = session.active_query_label
            message = "cancelled" if cancelled else "driver error"
            raise ValueError(message)

        monkeypatch.setattr("analytics_toolkit.sql_explorer.runtime.sql.read", fail)
        with pytest.raises(ValueError, match=r"cancelled|driver error"):
            session.export_dataframe()
        assert records(session.journal, "gp")[0]["outcome"] == (
            "cancelled" if cancelled else "failed"
        )


def test_observed_submission_records_cancellation_and_metadata_cancel_skips_idle(
    tmp_path: Path,
) -> None:
    journal = QueryJournal(tmp_path)
    scope = SqlCancellationScope()
    cancel_metadata(scope, journal, "gp", "gp")
    assert not journal.store("gp").path.exists()
    with pytest.raises(AsyncSqlCancelled), journal.action("gp", "gp", "user") as action:  # noqa: SIM117 -- Python 3.8 cannot parenthesize contexts.
        with action.submission("select slow()"):
            raise AsyncSqlCancelled
    record = records(journal, "gp")[0]
    assert record["submissions"][0]["outcome"] == "cancelled"


@pytest.mark.parametrize("native", [False, True])
def test_clickhouse_observer_sits_below_routing_without_duplicate_records(
    tmp_path: Path, native: bool
) -> None:
    calls = []

    class Native:
        def execute(self, query: str, **kwargs: Any) -> Any:
            calls.append(query)
            return ([(1,)], [("value", "UInt8")]) if kwargs.get("with_column_types") else None

    class Http:
        def command(self, query: str, **kwargs: Any) -> None:
            calls.append(query)

        def query(self, query: str, **kwargs: Any) -> None:
            calls.append(query)

    journal = QueryJournal(tmp_path)
    client = NativeClickHouseClient(Native()) if native else Http()
    config = SimpleNamespace(cluster_routing=ChClusterRouting(cluster="analytics"), database="db")
    with journal.action(
        "ch",
        "ch",
        "user",
        user_sql="CREATE TABLE db.events (id UInt64) ENGINE=MergeTree ORDER BY id",
    ):
        routed = wrap_client(client, config)
        assert observed_connection(routed) is routed
        routed.command("CREATE TABLE db.events (id UInt64) ENGINE=MergeTree ORDER BY id")
        routed.query("SELECT 1")
    submitted = [item["sql"] for item in records(journal, "ch")[0]["submissions"]]
    assert submitted == calls
    assert len(submitted) == 2
    assert "ON CLUSTER" in submitted[0]
    assert "ON CLUSTER" not in records(journal, "ch")[0]["user_sql"]
