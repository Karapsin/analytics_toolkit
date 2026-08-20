from __future__ import annotations

import __main__
import importlib
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace

import pytest

import analytics_toolkit.general as general
from analytics_toolkit.general import from_here, here, read_file_here, write_file
from analytics_toolkit.general.read_file import read_file
from analytics_toolkit.general.read_file import _find_unique_recursive_match
from analytics_toolkit.general.read_file import _looks_like_stdlib_path
from analytics_toolkit.general.read_file import _resolve_base_dir
from analytics_toolkit.general.read_file import _resolve_positron_editor_dir
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError


FrameInfo = namedtuple("FrameInfo", ["filename"])
READ_FILE_MODULE = importlib.import_module("analytics_toolkit.general.read_file")
RUNTIME_STACK = [
    FrameInfo(filename="/Users/test/project/utils_dev/analytics_toolkit/general/read_file.py"),
    FrameInfo(filename="/private/var/folders/vq/zns5cfbd6zd64jw8hfgzzczr0000gq/T/ipykernel_99706/123.py"),
    FrameInfo(filename="/Users/test/.venv/lib/python3.11/site-packages/IPython/core/interactiveshell.py"),
    FrameInfo(filename="/opt/homebrew/Cellar/python@3.14/3.14.3_1/Frameworks/Python.framework/Versions/3.14/lib/python3.14/asyncio/events.py"),
]


def _mock_stack(monkeypatch, frames: list[FrameInfo]) -> None:
    monkeypatch.setattr("analytics_toolkit.general.read_file.inspect.stack", lambda: frames)


def _mock_positron_parent(monkeypatch, parent: object) -> None:
    class FakeKernel:
        def get_parent(self, channel: str) -> object:
            assert channel == "shell"
            return parent

    fake_ipython = SimpleNamespace(
        get_ipython=lambda: SimpleNamespace(kernel=FakeKernel()),
    )
    monkeypatch.setattr(
        READ_FILE_MODULE,
        "import_module",
        lambda name: fake_ipython if name == "IPython" else importlib.import_module(name),
    )


def _positron_parent(uri: object) -> dict[str, object]:
    return {
        "content": {
            "positron": {
                "code_location": {
                    "uri": uri,
                },
            },
        },
    }


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


def test_positron_editor_location_precedes_main_stack_and_cwd(
    monkeypatch,
    tmp_path: Path,
) -> None:
    editor_file = tmp_path / "editor" / "analysis.py"
    editor_file.parent.mkdir()
    stale_main = tmp_path / "stale" / "task.py"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(__main__, "__file__", str(stale_main), raising=False)
    _mock_stack(
        monkeypatch,
        [
            FrameInfo(filename="/project/analytics_toolkit/general/read_file.py"),
            FrameInfo(filename="/unrelated/runtime/caller.py"),
        ],
    )
    _mock_positron_parent(monkeypatch, _positron_parent(editor_file.as_uri()))

    assert _resolve_base_dir() == editor_file.parent


def test_positron_editor_location_decodes_percent_encoded_path(
    monkeypatch,
    tmp_path: Path,
) -> None:
    editor_file = tmp_path / "project with spaces" / "analysis file.py"
    _mock_positron_parent(monkeypatch, _positron_parent(editor_file.as_uri()))

    assert _resolve_positron_editor_dir() == editor_file.parent


def test_positron_editor_location_preserves_file_uri_netloc(monkeypatch) -> None:
    _mock_positron_parent(
        monkeypatch,
        _positron_parent("file://fileserver/project%20share/analysis.py"),
    )

    assert _resolve_positron_editor_dir() == Path("//fileserver/project share")


@pytest.mark.parametrize(
    "parent",
    [
        None,
        {},
        {"content": None},
        {"content": {"positron": None}},
        {"content": {"positron": {"code_location": None}}},
        _positron_parent(None),
        _positron_parent("https://example.com/analysis.py"),
        _positron_parent("file:"),
        _positron_parent("file://[invalid"),
    ],
)
def test_positron_editor_location_rejects_missing_or_malformed_metadata(
    monkeypatch,
    parent: object,
) -> None:
    _mock_positron_parent(monkeypatch, parent)

    assert _resolve_positron_editor_dir() is None


def test_positron_editor_location_handles_missing_ipython(monkeypatch) -> None:
    def missing_ipython(name: str) -> object:
        if name == "IPython":
            raise ModuleNotFoundError(name)
        return importlib.import_module(name)

    monkeypatch.setattr(READ_FILE_MODULE, "import_module", missing_ipython)

    assert _resolve_positron_editor_dir() is None


def test_positron_editor_location_handles_missing_get_ipython(monkeypatch) -> None:
    monkeypatch.setattr(
        READ_FILE_MODULE,
        "import_module",
        lambda name: SimpleNamespace(),
    )

    assert _resolve_positron_editor_dir() is None


@pytest.mark.parametrize(
    "shell",
    [
        None,
        SimpleNamespace(),
        SimpleNamespace(kernel=None),
        SimpleNamespace(kernel=SimpleNamespace()),
    ],
)
def test_positron_editor_location_handles_missing_shell_or_kernel(
    monkeypatch,
    shell: object,
) -> None:
    fake_ipython = SimpleNamespace(get_ipython=lambda: shell)
    monkeypatch.setattr(READ_FILE_MODULE, "import_module", lambda name: fake_ipython)

    assert _resolve_positron_editor_dir() is None


def test_positron_editor_location_handles_runtime_access_exception(monkeypatch) -> None:
    class FailingKernel:
        def get_parent(self, channel: str) -> object:
            raise RuntimeError(channel)

    fake_ipython = SimpleNamespace(
        get_ipython=lambda: SimpleNamespace(kernel=FailingKernel()),
    )
    monkeypatch.setattr(READ_FILE_MODULE, "import_module", lambda name: fake_ipython)

    assert _resolve_positron_editor_dir() is None


def test_here_prefers_main_module_directory_for_new_paths(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(__main__, "__file__", str(Path(__file__).resolve()), raising=False)

    resolved = here("new_output.xlsx")

    assert resolved == str(Path(__file__).resolve().parent / "new_output.xlsx")


def test_here_falls_back_to_cwd_without_main_file(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delattr(__main__, "__file__", raising=False)
    _mock_stack(monkeypatch, RUNTIME_STACK)

    resolved = here("new_output.xlsx")

    assert resolved == str(tmp_path / "new_output.xlsx")


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


def test_resolve_base_dir_uses_first_real_caller_file(monkeypatch) -> None:
    fake_stack = [
        FrameInfo(filename="/Users/test/project/utils_dev/analytics_toolkit/general/read_file.py"),
        FrameInfo(filename="/private/var/folders/vq/zns5cfbd6zd64jw8hfgzzczr0000gq/T/ipykernel_99706/123.py"),
        FrameInfo(filename="/opt/homebrew/Cellar/python@3.14/3.14.3_1/Frameworks/Python.framework/Versions/3.14/lib/python3.14/asyncio/events.py"),
        FrameInfo(filename="/Users/test/project/tickets/april_2026/MAL-3657/compute_metrics.py"),
    ]
    monkeypatch.delattr(__main__, "__file__", raising=False)
    monkeypatch.setattr("analytics_toolkit.general.read_file.inspect.stack", lambda: fake_stack)

    resolved = _resolve_base_dir()

    assert resolved == Path("/Users/test/project/tickets/april_2026/MAL-3657")


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


def test_here_uses_first_real_caller_after_ide_runtime_frames(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delattr(__main__, "__file__", raising=False)
    _mock_stack(
        monkeypatch,
        [
            FrameInfo(filename="/Users/test/project/utils_dev/analytics_toolkit/general/read_file.py"),
            FrameInfo(filename="/Users/test/project/utils_dev/analytics_toolkit/general/read_file.py"),
            FrameInfo(filename="/Users/test/.vscode/extensions/ms-python.python/pythonFiles/lib/python/debugpy/launcher/__main__.py"),
            FrameInfo(filename="/Applications/PyCharm.app/Contents/plugins/python/helpers/pydev/pydevd.py"),
            FrameInfo(filename="/Users/test/.venv/lib/python3.11/site-packages/pydevd.py"),
            FrameInfo(filename="/opt/homebrew/Cellar/python@3.14/3.14.3_1/Frameworks/Python.framework/Versions/3.14/lib/python3.14/runpy.py"),
            FrameInfo(filename="/Users/test/project/reports/build_report.py"),
            FrameInfo(filename="/Users/test/project/reports/helpers.py"),
        ],
    )

    resolved = here("new_output.xlsx")

    assert resolved == "/Users/test/project/reports/new_output.xlsx"


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


def test_here_returns_cwd_path_for_missing_output_without_base_dir(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delattr(__main__, "__file__", raising=False)
    _mock_stack(monkeypatch, RUNTIME_STACK)

    resolved = here("new_output.xlsx")

    assert resolved == str(tmp_path / "new_output.xlsx")


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


def test_stdlib_shape_detection_rejects_nonstdlib_python_modules() -> None:
    assert _looks_like_stdlib_path(Path("/opt/lib/python3.14/asyncio/events.py"))
    assert not _looks_like_stdlib_path(Path("/opt/lib/python3.14/vendor/events.py"))


def test_here_does_not_recursively_match_an_absolute_path(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delattr(__main__, "__file__", raising=False)
    _mock_stack(monkeypatch, RUNTIME_STACK)
    absolute = tmp_path.parent / "not-present" / "query.sql"

    assert here(str(absolute)) == str(absolute)


def test_from_here_zero_matches_here_with_base_dir(
    monkeypatch,
    tmp_path: Path,
) -> None:
    script_dir = tmp_path / "project" / "dags" / "tasks"
    script_dir.mkdir(parents=True)
    monkeypatch.setitem(read_file.__globals__, "_resolve_base_dir", lambda: script_dir)

    assert from_here("queries/orders.sql", 0) == here("queries/orders.sql")


def test_from_here_resolves_parent_directory(
    monkeypatch,
    tmp_path: Path,
) -> None:
    script_dir = tmp_path / "project" / "tasks"
    script_dir.mkdir(parents=True)
    monkeypatch.setitem(read_file.__globals__, "_resolve_base_dir", lambda: script_dir)

    resolved = from_here(".connections", 1)

    assert resolved == str(tmp_path / "project" / ".connections")


def test_from_here_resolves_multiple_parent_levels(
    monkeypatch,
    tmp_path: Path,
) -> None:
    script_dir = tmp_path / "project" / "dags" / "tasks"
    script_dir.mkdir(parents=True)
    monkeypatch.setitem(read_file.__globals__, "_resolve_base_dir", lambda: script_dir)

    resolved = from_here(".connections", 2)

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


def test_from_here_rejects_negative_levels() -> None:
    with pytest.raises(ValueError, match="levels_up"):
        from_here(".connections", -1)


@pytest.mark.parametrize("levels_up", [1.0, "1", True])
def test_from_here_rejects_non_integer_levels(levels_up: object) -> None:
    with pytest.raises(TypeError, match="levels_up"):
        from_here(".connections", levels_up)  # type: ignore[arg-type]


def test_general_path_helper_exports_are_compatible() -> None:
    assert general.here is here
    assert general.from_here is from_here
    assert general.read_file_here is read_file_here
    assert general.read_file.inspect is not None
    assert "from_here" in general.__all__
    assert "read_file_here" in general.__all__


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


def test_read_file_can_resolve_relative_to_here(
    monkeypatch,
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "runtime"
    script_dir = tmp_path / "dag_task"
    query_dir = script_dir / "sql"
    cwd.mkdir()
    query_dir.mkdir(parents=True)
    (query_dir / "query.sql").write_text("select {value}", encoding="utf-8")
    monkeypatch.chdir(cwd)
    monkeypatch.setitem(read_file.__globals__, "_resolve_base_dir", lambda: script_dir)

    assert read_file("sql/query.sql", {"value": 1}, here=True) == "select 1"


def test_read_file_here_matches_read_file_with_here(
    monkeypatch,
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "runtime"
    script_dir = tmp_path / "dag_task"
    query_dir = script_dir / "sql"
    cwd.mkdir()
    query_dir.mkdir(parents=True)
    (query_dir / "query.sql").write_text("select 1", encoding="utf-8")
    monkeypatch.chdir(cwd)
    monkeypatch.setitem(read_file.__globals__, "_resolve_base_dir", lambda: script_dir)

    assert read_file_here("sql/query.sql") == read_file("sql/query.sql", here=True)


def test_read_file_here_preserves_params(
    monkeypatch,
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "runtime"
    script_dir = tmp_path / "dag_task"
    query_dir = script_dir / "sql"
    cwd.mkdir()
    query_dir.mkdir(parents=True)
    (query_dir / "query.sql").write_text("select {value}", encoding="utf-8")
    monkeypatch.chdir(cwd)
    monkeypatch.setitem(read_file.__globals__, "_resolve_base_dir", lambda: script_dir)

    assert read_file_here("sql/query.sql", {"value": 42}) == "select 42"


def test_read_file_rejects_nonexistent_path(tmp_path: Path) -> None:
    missing = tmp_path / "missing.sql"

    with pytest.raises(InvalidSqlInputError, match="does not exist"):
        read_file(str(missing))


def test_write_file_writes_utf8_text(tmp_path: Path) -> None:
    output = tmp_path / "result.txt"

    write_file(output, "select 'привет'")

    assert output.read_text(encoding="utf-8") == "select 'привет'"
    assert read_file(output) == "select 'привет'"


def test_write_file_creates_parent_directories(tmp_path: Path) -> None:
    output = tmp_path / "nested" / "reports" / "result.txt"

    write_file(output, "done")

    assert output.read_text(encoding="utf-8") == "done"


def test_general_write_file_export_is_compatible() -> None:
    assert general.write_file is write_file
    assert "write_file" in general.__all__
