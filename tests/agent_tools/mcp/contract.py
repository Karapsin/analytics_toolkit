from __future__ import annotations

from tests.agent_tools._support.mcp import (
    mcp_server,
)


def test_sql_focused_checks_include_all_sql_unit_modules() -> None:
    command = mcp_server._check_commands(
        area="sql",
        change_type="implementation",
        level="focused",
        root=mcp_server.REPO_ROOT,
    )[0]

    assert command["args"] == ["pytest", "-q", "tests/sql"]
