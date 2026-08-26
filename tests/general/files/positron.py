from __future__ import annotations

from tests.general._support.files import (
    READ_FILE_MODULE,
    FrameInfo,
    Path,
    SimpleNamespace,
    __main__,
    _mock_positron_parent,
    _mock_stack,
    _positron_parent,
    _resolve_base_dir,
    _resolve_positron_editor_dir,
    importlib,
    pytest,
)


def test_positron_editor_location_decodes_percent_encoded_path(
    monkeypatch,
    tmp_path: Path,
) -> None:
    editor_file = tmp_path / "project with spaces" / "analysis file.py"
    _mock_positron_parent(monkeypatch, _positron_parent(editor_file.as_uri()))

    assert _resolve_positron_editor_dir() == editor_file.parent


def test_positron_editor_location_handles_missing_get_ipython(monkeypatch) -> None:
    monkeypatch.setattr(
        READ_FILE_MODULE,
        "import_module",
        lambda name: SimpleNamespace(),
    )

    assert _resolve_positron_editor_dir() is None


def test_positron_editor_location_handles_missing_ipython(monkeypatch) -> None:
    def missing_ipython(name: str) -> object:
        if name == "IPython":
            raise ModuleNotFoundError(name)
        return importlib.import_module(name)

    monkeypatch.setattr(READ_FILE_MODULE, "import_module", missing_ipython)

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
