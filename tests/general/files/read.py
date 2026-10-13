from __future__ import annotations

from tests.general._support.files import (
    InvalidSqlInputError,
    Path,
    __main__,
    from_here,
    general,
    here,
    pytest,
    read_file,
    read_file_here,
)
from tests.general._support.projects import script_query_project


def test_general_path_helper_exports_are_compatible() -> None:
    assert general.here is here
    assert general.from_here is from_here
    assert general.read_file_here is read_file_here
    assert general.read_file.inspect is not None
    assert "from_here" in general.__all__
    assert "read_file_here" in general.__all__


def test_read_file_can_resolve_relative_to_here(
    monkeypatch,
    tmp_path: Path,
) -> None:
    script_query_project(tmp_path, monkeypatch, "select {value}")

    assert read_file("sql/query.sql", {"value": 1}, here=True) == "select 1"


def test_read_file_here_matches_read_file_with_here(
    monkeypatch,
    tmp_path: Path,
) -> None:
    script_query_project(tmp_path, monkeypatch, "select 1")

    assert read_file_here("sql/query.sql") == read_file("sql/query.sql", here=True)


def test_read_file_here_preserves_params(
    monkeypatch,
    tmp_path: Path,
) -> None:
    script_query_project(tmp_path, monkeypatch, "select {value}")

    assert read_file_here("sql/query.sql", {"value": 42}) == "select 42"


def test_read_file_keeps_cwd_relative_default(monkeypatch, tmp_path: Path) -> None:
    cwd = tmp_path / "runtime"
    script_dir = tmp_path / "dag_task"
    cwd_sql = cwd / "sql"
    script_sql = script_dir / "sql"
    cwd_sql.mkdir(parents=True)
    script_sql.mkdir(parents=True)
    (cwd_sql / "query.sql").write_text("select 'cwd'", encoding="utf-8")
    (script_sql / "query.sql").write_text("select 'script'", encoding="utf-8")
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(__main__, "__file__", str(script_dir / "task.py"), raising=False)

    assert read_file("sql/query.sql") == "select 'cwd'"


def test_read_file_rejects_nonexistent_path(tmp_path: Path) -> None:
    missing = tmp_path / "missing.sql"

    with pytest.raises(InvalidSqlInputError, match="does not exist"):
        read_file(str(missing))
