from __future__ import annotations

from tests.general._support.files import (
    Path,
    general,
    read_file,
    write_file,
)


def test_general_write_file_export_is_compatible() -> None:
    assert general.write_file is write_file
    assert "write_file" in general.__all__


def test_write_file_creates_parent_directories(tmp_path: Path) -> None:
    output = tmp_path / "nested" / "reports" / "result.txt"

    write_file(output, "done")

    assert output.read_text(encoding="utf-8") == "done"


def test_write_file_writes_utf8_text(tmp_path: Path) -> None:
    output = tmp_path / "result.txt"

    write_file(output, "select 'привет'")

    assert output.read_text(encoding="utf-8") == "select 'привет'"
    assert read_file(output) == "select 'привет'"
