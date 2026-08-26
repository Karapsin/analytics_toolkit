from __future__ import annotations

from tests.sql._support.adapters import (
    SimpleNamespace,
    get_backend_adapter,
    pytest,
)


def test_dbapi_backend_adapter_rolls_back_failed_committed_commands() -> None:
    class FailingCursor:
        def __init__(self, connection: FailingConnection) -> None:
            self.connection = connection

        def execute(self, sql: str) -> None:
            self.connection.executed.append(sql)
            raise RuntimeError("boom")

        def close(self) -> None:
            self.connection.cursor_closed = True

    class FailingConnection:
        def __init__(self) -> None:
            self.executed: list[str] = []
            self.commit_calls = 0
            self.rollback_calls = 0
            self.cursor_closed = False

        def cursor(self) -> FailingCursor:
            return FailingCursor(self)

        def commit(self) -> None:
            self.commit_calls += 1

        def rollback(self) -> None:
            self.rollback_calls += 1

    connection = FailingConnection()

    try:
        get_backend_adapter("gp").execute_command(connection, "DROP TABLE target")
    except RuntimeError:
        pass
    else:
        raise AssertionError("Expected failing execute to raise.")

    assert connection.executed == ["DROP TABLE target"]
    assert connection.commit_calls == 0
    assert connection.rollback_calls == 1
    assert connection.cursor_closed is True


def test_dbapi_execute_commands_rolls_back_and_closes_on_later_failure() -> None:
    command_error = RuntimeError("command failed")

    class Cursor:
        def __init__(self, connection: Connection) -> None:
            self.connection = connection

        def execute(self, sql: str) -> None:
            self.connection.executed.append(sql)
            if sql == "bad":
                raise command_error

        def close(self) -> None:
            self.connection.closed = True

    class Connection:
        def __init__(self) -> None:
            self.executed: list[str] = []
            self.rollback_calls = 0
            self.closed = False

        def cursor(self) -> Cursor:
            return Cursor(self)

        def rollback(self) -> None:
            self.rollback_calls += 1

    connection = Connection()

    with pytest.raises(RuntimeError, match="command failed"):
        get_backend_adapter("gp").execute_commands(connection, ["good", "bad"])

    assert connection.executed == ["good", "bad"]
    assert connection.rollback_calls == 1
    assert connection.closed is True


def test_dbapi_insert_from_query_rolls_back_failed_committed_insert() -> None:
    insert_error = RuntimeError("insert failed")

    class Cursor:
        def __init__(self, connection: Connection) -> None:
            self.connection = connection

        def execute(self, sql: str) -> None:
            self.connection.sql = sql
            raise insert_error

        def close(self) -> None:
            self.connection.closed = True

    class Connection:
        def __init__(self) -> None:
            self.sql = ""
            self.rollback_calls = 0
            self.closed = False

        def cursor(self) -> Cursor:
            return Cursor(self)

        def rollback(self) -> None:
            self.rollback_calls += 1

    connection = Connection()

    with pytest.raises(RuntimeError, match="insert failed"):
        get_backend_adapter("gp").insert_from_query(
            connection,
            "target",
            "SELECT id FROM source",
            {"id": "BIGINT"},
        )

    assert connection.sql.startswith("INSERT INTO target")
    assert connection.rollback_calls == 1
    assert connection.closed is True


def test_noncommitting_dbapi_failures_do_not_require_rollback() -> None:
    execute_error = RuntimeError("failed")

    class Cursor:
        def __init__(self) -> None:
            self.closed = False

        def execute(self, sql: str) -> None:
            raise execute_error

        def close(self) -> None:
            self.closed = True

    cursor = Cursor()
    connection = SimpleNamespace(cursor=lambda: cursor)
    adapter = get_backend_adapter("trino")

    with pytest.raises(RuntimeError, match="failed"):
        adapter.execute_command(connection, "SELECT 1")
    assert cursor.closed is True

    cursor.closed = False
    with pytest.raises(RuntimeError, match="failed"):
        adapter.execute_commands(connection, ["SELECT 2"])
    assert cursor.closed is True
