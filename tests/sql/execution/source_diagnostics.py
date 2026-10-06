from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

import pandas as pd
from analytics_toolkit import sql
from analytics_toolkit.sql.connection.errors import (
    SqlConfigError,
    UnsupportedConnectionTypeError,
)

if TYPE_CHECKING:
    from pathlib import Path
from tests.sql._support.connection_config import (
    config_module,
    general_module,
    install_fake_airflow,
    json,
    operation_runner_module,
    pytest,
    sys,
    types,
)


def test_unknown_keys_and_validation_results_include_cached_absolute_path(tmp_path: Path) -> None:
    path = tmp_path / ".connections"
    config_module.get_connection_config("gp")
    path.unlink()

    with pytest.raises(UnsupportedConnectionTypeError) as caught:
        sql.read("missing", "select 1", retry_cnt=1, timeout_increment=0)
    assert str(path) in str(caught.value)
    assert "Available keys: ch, gp, gp_sandbox, trino" in str(caught.value)
    results = sql.validate_connections(["missing"])
    assert str(path) in results[0].error


@pytest.mark.parametrize("kind", ["duplicate", "invalid_field", "missing_reference"])
def test_configuration_failures_include_path_without_values(tmp_path: Path, kind: str) -> None:
    path = tmp_path / ".connections"
    entry: dict[str, Any] = {
        "type": "gp",
        "host": "host",
        "user": "user",
        "password": "private-password",
        "database": "db",
    }
    if kind == "duplicate":
        content = {"gp": entry, " GP ": entry}
    elif kind == "invalid_field":
        entry["port"] = "not-a-port"
        content = {"gp": entry}
    else:
        entry["password"] = {"from": "env", "key": "ABSENT_SECRET_FOR_TEST"}
        content = {"gp": entry}
    path.write_text(json.dumps(content), encoding="utf-8")

    with pytest.raises(SqlConfigError) as caught:
        config_module.get_connection_config("gp")

    assert str(path) in str(caught.value)
    assert "private-password" not in str(caught.value)


@pytest.mark.parametrize("helper", ["read", "execute", "load", "transfer"])
def test_connection_failures_preserve_driver_exception_and_show_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    helper: str,
) -> None:
    original = ConnectionError("fake driver failure")
    original.__cause__ = LookupError("original cause")

    def connect(**kwargs: Any) -> Any:
        raise original

    monkeypatch.setitem(sys.modules, "psycopg2", types.SimpleNamespace(connect=connect))
    operations = {
        "read": lambda: sql.read("gp", "select 1", retry_cnt=1, timeout_increment=0),
        "execute": lambda: sql.execute("gp", "select 1", retry_cnt=1, timeout_increment=0),
        "load": lambda: sql.load_df(
            "gp", "schema.target", pd.DataFrame({"id": [1]}), retry_cnt=1, timeout_increment=0
        ),
        "transfer": lambda: sql.transfer(
            "gp",
            "gp_sandbox",
            from_sql="select 1",
            to_table="schema.target",
            retry_cnt=1,
            timeout_increment=0,
            full_retry_cnt=1,
            full_timeout_increment=0,
        ),
    }
    with pytest.raises(ConnectionError) as caught:
        operations[helper]()

    assert caught.value is original
    assert isinstance(original.__cause__, LookupError)
    note = f"SQL connections file: {tmp_path / '.connections'}"
    assert original.__notes__.count(note) == 1
    assert capsys.readouterr().out.count(note) == 1


def test_query_failure_keeps_operation_metadata_and_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_module = importlib.import_module("analytics_toolkit.sql.dml.io.read_sql")
    original = RuntimeError("query failed")

    def execute(_query: str) -> None:
        raise original

    cursor = types.SimpleNamespace(execute=execute, close=lambda: None)
    connection = types.SimpleNamespace(
        cursor=lambda: cursor, close=lambda: None, rollback=lambda: None
    )
    monkeypatch.setattr(read_module, "get_sql_connection", lambda key: connection)
    with pytest.raises(RuntimeError) as caught:
        sql.read("gp", "select 1", retry_cnt=1, timeout_increment=0)

    assert caught.value is original
    assert original.sql_context.alias == "gp"
    assert f"SQL connections file: {tmp_path / '.connections'}" in original.__notes__


def test_batch_failure_and_items_keep_structured_errors_and_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def connect(**kwargs: Any) -> Any:
        message = "fake driver failure"
        raise ConnectionError(message)

    monkeypatch.setitem(sys.modules, "psycopg2", types.SimpleNamespace(connect=connect))
    with pytest.raises(sql.SqlBatchExecutionError) as caught:
        sql.execute("gp", ["select 1", "select 2"], retry_cnt=1, timeout_increment=0, concurrency=2)

    note = f"SQL connections file: {tmp_path / '.connections'}"
    assert note in caught.value.__notes__
    assert caught.value.failed_indexes == (0, 1)
    assert all(isinstance(item.error, ConnectionError) for item in caught.value.items)
    assert all(note in item.error.__notes__ for item in caught.value.items)


def test_fileless_airflow_failure_does_not_report_old_cached_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_module.get_connection_config("gp")
    install_fake_airflow(monkeypatch, {})
    with config_module.use_airflow_connections():  # noqa: SIM117 -- Python 3.8
        with pytest.raises(UnsupportedConnectionTypeError) as caught:
            sql.read("missing", "select 1", retry_cnt=1, timeout_increment=0)

    assert str(tmp_path / ".connections") not in str(caught.value)
    assert not any(
        "SQL connections file:" in note for note in getattr(caught.value, "__notes__", ())
    )
    assert "SQL connections file:" not in capsys.readouterr().out


def test_no_discovered_source_does_not_fabricate_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config_module, "find_connections_file_path", lambda: None)

    def unexpected_connect(**kwargs: Any) -> Any:
        pytest.fail("Missing-file lookup must not open a database connection")

    monkeypatch.setitem(sys.modules, "psycopg2", types.SimpleNamespace(connect=unexpected_connect))
    general_module.set_connections_path(None)
    with pytest.raises(SqlConfigError, match="Missing SQL connections file") as caught:
        sql.read("gp", "select 1", retry_cnt=1, timeout_increment=0)

    assert "[SQL connections file:" not in str(caught.value)
    assert not getattr(caught.value, "__notes__", ())


def test_diagnostic_sink_failure_does_not_replace_driver_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    diagnostics = importlib.import_module("analytics_toolkit.sql.connection.config_diagnostics")
    config_module.get_connection_config("gp")
    original = RuntimeError("driver failure")

    def failing_sink(*args: Any, **kwargs: Any) -> None:
        message = "sink failed"
        raise ValueError(message)

    monkeypatch.setattr(diagnostics, "time_print", failing_sink)

    @operation_runner_module.timed_public_sql_function
    def operation() -> None:
        raise original

    with pytest.raises(RuntimeError) as caught:
        operation()
    assert caught.value is original


def test_driver_rejecting_notes_still_logs_path_and_preserves_exception(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class DriverError(RuntimeError):
        def add_note(self, note: str) -> None:
            message = "Driver does not support notes"
            raise ValueError(message)

    config_module.get_connection_config("gp")
    original = DriverError("driver failure")

    @operation_runner_module.timed_public_sql_function
    def operation() -> None:
        raise original

    with pytest.raises(DriverError) as caught:
        operation()
    assert caught.value is original
    assert f"SQL connections file: {tmp_path / '.connections'}" in capsys.readouterr().out


def test_failure_reports_source_used_before_explicit_path_change(tmp_path: Path) -> None:
    original_path = tmp_path / ".connections"
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other_path = other_dir / ".connections"
    other_path.write_text("{}", encoding="utf-8")

    @operation_runner_module.timed_public_sql_function
    def operation() -> None:
        config_module.get_connection_config("gp")
        general_module.set_connections_path(other_path)
        message = "query failure"
        raise RuntimeError(message)

    with pytest.raises(RuntimeError) as caught:
        operation()

    assert f"SQL connections file: {original_path}" in caught.value.__notes__
    assert str(other_path) not in "\n".join(caught.value.__notes__)
