from __future__ import annotations

from tests.sql._support.cross_area import (
    FakeDbapiConnection,
    execute_read_module,
    pytest,
    read_sql_module,
)


@pytest.mark.parametrize(
    "source",
    [
        "select 1;\n-- comment",
        "select 1; -- comment",
        "select 1\n-- comment",
        "select 1\n/* comment */",
        "select 1; /* comment */",
    ],
)
def test_public_read_and_execute_read_accept_trailing_comments(monkeypatch, source: str) -> None:
    for module, method in [(read_sql_module, "read_sql"), (execute_read_module, "execute_read")]:
        connection = FakeDbapiConnection(rows=[(1,)], description=[("value",)])
        monkeypatch.setattr(
            module, "get_sql_connection", lambda key, connection=connection: connection
        )
        result = getattr(module, method)("gp", source, retry_cnt=1, timeout_increment=0)
        assert result["value"].tolist() == [1]
        assert len(connection.executed) == 1
        assert "comment" in connection.executed[0]
