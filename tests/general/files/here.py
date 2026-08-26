from __future__ import annotations

from tests.general._support.files import (
    READ_FILE_MODULE,
    RUNTIME_STACK,
    FrameInfo,
    Path,
    __main__,
    _find_unique_recursive_match,
    _looks_like_stdlib_path,
    _mock_positron_parent,
    _mock_stack,
    _positron_parent,
    _resolve_base_dir,
    from_here,
    here,
    pytest,
    read_file,
)


def test_from_here_rejects_negative_levels() -> None:
    with pytest.raises(ValueError, match="levels_up"):
        from_here(".connections", -1)


@pytest.mark.parametrize("levels_up", [1.0, "1", True])
def test_from_here_rejects_non_integer_levels(levels_up: object) -> None:
    with pytest.raises(TypeError, match="levels_up"):
        from_here(".connections", levels_up)  # type: ignore[arg-type]


def test_from_here_resolves_multiple_parent_levels(
    monkeypatch,
    tmp_path: Path,
) -> None:
    script_dir = tmp_path / "project" / "dags" / "tasks"
    script_dir.mkdir(parents=True)
    monkeypatch.setitem(read_file.__globals__, "_resolve_base_dir", lambda: script_dir)

    resolved = from_here(".connections", 2)

    assert resolved == str(tmp_path / "project" / ".connections")


def test_from_here_resolves_parent_directory(
    monkeypatch,
    tmp_path: Path,
) -> None:
    script_dir = tmp_path / "project" / "tasks"
    script_dir.mkdir(parents=True)
    monkeypatch.setitem(read_file.__globals__, "_resolve_base_dir", lambda: script_dir)

    resolved = from_here(".connections", 1)

    assert resolved == str(tmp_path / "project" / ".connections")


def test_from_here_uses_cwd_without_base_dir(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delattr(__main__, "__file__", raising=False)
    _mock_stack(monkeypatch, RUNTIME_STACK)

    resolved = from_here(".connections", 1)

    assert resolved == str(tmp_path.parent / ".connections")


def test_from_here_zero_matches_here_with_base_dir(
    monkeypatch,
    tmp_path: Path,
) -> None:
    script_dir = tmp_path / "project" / "dags" / "tasks"
    script_dir.mkdir(parents=True)
    monkeypatch.setitem(read_file.__globals__, "_resolve_base_dir", lambda: script_dir)

    assert from_here("queries/orders.sql", 0) == here("queries/orders.sql")


def test_here_ambiguous_recursive_relative_matches_fall_back_to_cwd(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delattr(__main__, "__file__", raising=False)
    _mock_stack(monkeypatch, RUNTIME_STACK)
    for directory in ("project_a", "project_b"):
        path = tmp_path / directory / "sql" / "query.sql"
        path.parent.mkdir(parents=True)
        path.write_text("select 1", encoding="utf-8")

    resolved = here("sql/query.sql")

    assert resolved == str(tmp_path / "sql" / "query.sql")


def test_here_does_not_match_unrelated_unique_basename_for_relative_path(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delattr(__main__, "__file__", raising=False)
    _mock_stack(monkeypatch, RUNTIME_STACK)
    unrelated = tmp_path / "other" / "query.sql"
    unrelated.parent.mkdir()
    unrelated.write_text("select 1", encoding="utf-8")

    resolved = here("sql/query.sql")

    assert resolved == str(tmp_path / "sql" / "query.sql")


def test_here_does_not_recursively_match_an_absolute_path(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delattr(__main__, "__file__", raising=False)
    _mock_stack(monkeypatch, RUNTIME_STACK)
    absolute = tmp_path.parent / "not-present" / "query.sql"

    assert here(str(absolute)) == str(absolute)


def test_here_falls_back_to_cwd_without_main_file(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delattr(__main__, "__file__", raising=False)
    _mock_stack(monkeypatch, RUNTIME_STACK)

    resolved = here("new_output.xlsx")

    assert resolved == str(tmp_path / "new_output.xlsx")


def test_here_keeps_unique_basename_recursive_lookup_for_compatibility(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delattr(__main__, "__file__", raising=False)
    _mock_stack(monkeypatch, RUNTIME_STACK)
    expected = tmp_path / "nested" / "query.sql"
    expected.parent.mkdir()
    expected.write_text("select 1", encoding="utf-8")

    resolved = here("query.sql")

    assert resolved == str(expected)


def test_here_prefers_main_module_directory_for_new_paths(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(__main__, "__file__", str(Path(__file__).resolve()), raising=False)

    resolved = here("new_output.xlsx")

    assert resolved == str(Path(__file__).resolve().parent / "new_output.xlsx")


def test_here_recursively_resolves_unique_relative_path(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delattr(__main__, "__file__", raising=False)
    _mock_stack(monkeypatch, RUNTIME_STACK)
    expected = tmp_path / "project_a" / "sql" / "query.sql"
    expected.parent.mkdir(parents=True)
    expected.write_text("select 1", encoding="utf-8")

    resolved = here("sql/query.sql")

    assert resolved == str(expected)


def test_here_returns_cwd_path_for_missing_output_without_base_dir(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delattr(__main__, "__file__", raising=False)
    _mock_stack(monkeypatch, RUNTIME_STACK)

    resolved = here("new_output.xlsx")

    assert resolved == str(tmp_path / "new_output.xlsx")


def test_here_returns_existing_cwd_file_without_base_dir(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delattr(__main__, "__file__", raising=False)
    _mock_stack(monkeypatch, RUNTIME_STACK)
    expected = tmp_path / "existing.sql"
    expected.write_text("select 1", encoding="utf-8")

    resolved = here("existing.sql")

    assert resolved == str(expected)


def test_here_uses_first_real_caller_after_ide_runtime_frames(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delattr(__main__, "__file__", raising=False)
    _mock_stack(
        monkeypatch,
        [
            FrameInfo(
                filename="/Users/test/project/utils_dev/analytics_toolkit/general/read_file.py"
            ),
            FrameInfo(
                filename="/Users/test/project/utils_dev/analytics_toolkit/general/read_file.py"
            ),
            FrameInfo(
                filename="/Users/test/.vscode/extensions/ms-python.python/pythonFiles/lib/python/debugpy/launcher/__main__.py"
            ),
            FrameInfo(
                filename="/Applications/PyCharm.app/Contents/plugins/python/helpers/pydev/pydevd.py"
            ),
            FrameInfo(filename="/Users/test/.venv/lib/python3.11/site-packages/pydevd.py"),
            FrameInfo(
                filename="/opt/homebrew/Cellar/python@3.14/3.14.3_1/Frameworks/Python.framework/Versions/3.14/lib/python3.14/runpy.py"
            ),
            FrameInfo(filename="/Users/test/project/reports/build_report.py"),
            FrameInfo(filename="/Users/test/project/reports/helpers.py"),
        ],
    )

    resolved = here("new_output.xlsx")

    assert resolved == "/Users/test/project/reports/new_output.xlsx"


def test_here_uses_positron_editor_location_without_editor_stack_frame(
    monkeypatch,
    tmp_path: Path,
) -> None:
    editor_file = tmp_path / "project" / "analysis.py"
    editor_file.parent.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.delattr(__main__, "__file__", raising=False)
    _mock_stack(
        monkeypatch,
        [
            FrameInfo(filename="/project/analytics_toolkit/general/read_file.py"),
            FrameInfo(filename="<positron-console-cell-1>"),
            *RUNTIME_STACK[1:],
        ],
    )
    _mock_positron_parent(monkeypatch, _positron_parent(editor_file.as_uri()))

    assert here("output.csv") == str(editor_file.parent / "output.csv")


def test_here_uses_real_caller_when_main_file_is_ipykernel(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        __main__,
        "__file__",
        "/private/var/folders/vq/zns5cfbd6zd64jw8hfgzzczr0000gq/T/ipykernel_99706/123.py",
        raising=False,
    )
    _mock_stack(
        monkeypatch,
        [
            *RUNTIME_STACK,
            FrameInfo(filename="/Users/test/project/notebooks/analysis.py"),
        ],
    )

    resolved = here("new_output.xlsx")

    assert resolved == "/Users/test/project/notebooks/new_output.xlsx"


def test_resolve_base_dir_skips_other_analytics_toolkit_frames(monkeypatch) -> None:
    package_dir = Path(READ_FILE_MODULE.__file__).resolve().parents[1]
    monkeypatch.delattr(__main__, "__file__", raising=False)
    _mock_stack(
        monkeypatch,
        [
            FrameInfo(filename=str(package_dir / "general" / "read_file.py")),
            FrameInfo(filename=str(package_dir / "sql" / "connection" / "config_path.py")),
            FrameInfo(filename="/project/dags/report.py"),
        ],
    )

    assert _resolve_base_dir() == Path("/project/dags")


def test_resolve_base_dir_skips_pseudo_and_stdlib_frames(monkeypatch) -> None:
    monkeypatch.delattr(__main__, "__file__", raising=False)
    _mock_stack(
        monkeypatch,
        [
            FrameInfo(filename="/project/analytics_toolkit/general/read_file.py"),
            FrameInfo(filename="<frozen runpy>"),
            FrameInfo(filename="/opt/python/lib/python3.14/asyncio/events.py"),
            FrameInfo(filename="/project/jobs/report.py"),
        ],
    )

    assert _resolve_base_dir() == Path("/project/jobs")


def test_resolve_base_dir_uses_first_real_caller_file(monkeypatch) -> None:
    fake_stack = [
        FrameInfo(filename="/Users/test/project/utils_dev/analytics_toolkit/general/read_file.py"),
        FrameInfo(
            filename="/private/var/folders/vq/zns5cfbd6zd64jw8hfgzzczr0000gq/T/ipykernel_99706/123.py"
        ),
        FrameInfo(
            filename="/opt/homebrew/Cellar/python@3.14/3.14.3_1/Frameworks/Python.framework/Versions/3.14/lib/python3.14/asyncio/events.py"
        ),
        FrameInfo(filename="/Users/test/project/tickets/april_2026/MAL-3657/compute_metrics.py"),
    ]
    monkeypatch.delattr(__main__, "__file__", raising=False)
    monkeypatch.setattr("analytics_toolkit.general.read_file.inspect.stack", lambda: fake_stack)

    resolved = _resolve_base_dir()

    assert resolved == Path("/Users/test/project/tickets/april_2026/MAL-3657")


def test_stdlib_shape_detection_rejects_nonstdlib_python_modules() -> None:
    assert _looks_like_stdlib_path(Path("/opt/lib/python3.14/asyncio/events.py"))
    assert not _looks_like_stdlib_path(Path("/opt/lib/python3.14/vendor/events.py"))


def test_unique_basename_fallback_returns_the_only_recursive_match() -> None:
    expected = Path("/project/nested/query.sql")

    class FakeCwd:
        def __init__(self) -> None:
            self.calls = 0

        def rglob(self, pattern: str):
            self.calls += 1
            assert pattern == "query.sql"
            return [] if self.calls == 1 else [expected]

    assert _find_unique_recursive_match(FakeCwd(), Path("query.sql")) == expected  # type: ignore[arg-type]
