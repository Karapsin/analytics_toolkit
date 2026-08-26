from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from release_routines.lib import quality_debt

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any


def test_parse_ruff_json_counts_by_relative_file_and_rule(tmp_path: Path) -> None:
    payload = [
        {
            "filename": str(tmp_path / "package" / "module.py"),
            "code": "E501",
        },
        {
            "filename": str(tmp_path / "package" / "module.py"),
            "code": "E501",
        },
        {
            "filename": str(tmp_path / "package" / "module.py"),
            "code": "F401",
        },
    ]

    findings = quality_debt.parse_ruff_json(json.dumps(payload), tmp_path)

    assert findings == {"package/module.py": {"E501": 2, "F401": 1}}


def test_parse_mypy_json_lines_ignores_notes(tmp_path: Path) -> None:
    rows = [
        {
            "file": "package/module.py",
            "code": "arg-type",
            "severity": "error",
        },
        {
            "file": "package/module.py",
            "code": "arg-type",
            "severity": "error",
        },
        {
            "file": "package/module.py",
            "code": "note",
            "severity": "note",
        },
    ]

    findings = quality_debt.parse_mypy_json_lines(
        "\n".join(json.dumps(row) for row in rows),
        tmp_path,
    )

    assert findings == {"package/module.py": {"arg-type": 2}}


def test_parse_ruff_format_output_records_each_unformatted_file(
    tmp_path: Path,
) -> None:
    output = (
        "Would reformat: package/a.py\nWould reformat: package/b.py\n2 files would be reformatted"
    )

    findings = quality_debt.parse_ruff_format_output(output, tmp_path)

    assert findings == {
        "package/a.py": {"FORMAT": 1},
        "package/b.py": {"FORMAT": 1},
    }


def test_compare_debt_fails_increases_and_allows_removals() -> None:
    comparison = quality_debt.compare_debt(
        {
            "package/a.py": {"E501": 2, "F401": 1},
            "package/removed.py": {"E501": 1},
        },
        {
            "package/a.py": {"E501": 1, "F401": 2},
            "package/new.py": {"E501": 1},
        },
    )

    assert comparison.baseline_total == 4
    assert comparison.current_total == 4
    assert comparison.resolved_total == 2
    assert comparison.current_buckets == 3
    assert comparison.increases == (
        quality_debt.DebtIncrease("package/a.py", "F401", 1, 2),
        quality_debt.DebtIncrease("package/new.py", "E501", 0, 1),
    )


def test_write_baseline_updates_one_tool_group_and_preserves_the_other(
    tmp_path: Path,
) -> None:
    baseline_path = tmp_path / "quality_debt.json"
    quality_debt.write_baseline(
        baseline_path,
        {"mypy": {"package/a.py": {"arg-type": 2}}},
    )
    quality_debt.write_baseline(
        baseline_path,
        {
            "ruff": {"package/a.py": {"E501": 1}},
            "ruff_format": {"package/a.py": {"FORMAT": 1}},
        },
    )

    baseline = quality_debt.load_baseline(baseline_path)

    assert baseline == {
        "mypy": {"package/a.py": {"arg-type": 2}},
        "ruff": {"package/a.py": {"E501": 1}},
        "ruff_format": {"package/a.py": {"FORMAT": 1}},
    }


def test_lint_gate_reports_remaining_debt_and_fails_new_findings(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    baseline_path = tmp_path / "quality_debt.json"
    quality_debt.write_baseline(
        baseline_path,
        {
            "ruff": {"package/a.py": {"E501": 1}},
            "ruff_format": {},
        },
    )

    def runner(command: Any, cwd: Path) -> quality_debt.CommandResult:
        del cwd
        if command[1] == "check":
            return quality_debt.CommandResult(
                returncode=1,
                stdout=json.dumps(
                    [
                        {
                            "filename": str(tmp_path / "package" / "a.py"),
                            "code": "E501",
                        },
                        {
                            "filename": str(tmp_path / "package" / "a.py"),
                            "code": "E501",
                        },
                    ]
                ),
                stderr="",
            )
        return quality_debt.CommandResult(
            returncode=1,
            stdout="Would reformat: package/new.py\n",
            stderr="",
        )

    result = quality_debt.run_gate(
        "lint",
        root=tmp_path,
        baseline_path=baseline_path,
        write=False,
        runner=runner,
    )

    output = capsys.readouterr().out
    assert result == 1
    assert "baseline debt finding(s) remain" in output
    assert "package/a.py [E501]" in output
    assert "package/new.py [FORMAT]" in output


def test_type_gate_allows_reduced_debt(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    baseline_path = tmp_path / "quality_debt.json"
    quality_debt.write_baseline(
        baseline_path,
        {"mypy": {"package/a.py": {"arg-type": 2}}},
    )

    def runner(command: Any, cwd: Path) -> quality_debt.CommandResult:
        del command, cwd
        row = {
            "file": "package/a.py",
            "code": "arg-type",
            "severity": "error",
        }
        return quality_debt.CommandResult(
            returncode=1,
            stdout=json.dumps(row),
            stderr="",
        )

    result = quality_debt.run_gate(
        "type",
        root=tmp_path,
        baseline_path=baseline_path,
        write=False,
        runner=runner,
    )

    output = capsys.readouterr().out
    assert result == 0
    assert "1 baseline debt finding(s) remain" in output
    assert "1 finding(s) were removed" in output


def test_gate_rejects_unrecognized_tool_failures(tmp_path: Path) -> None:
    def runner(command: Any, cwd: Path) -> quality_debt.CommandResult:
        del command, cwd
        return quality_debt.CommandResult(
            returncode=2,
            stdout="",
            stderr="configuration error",
        )

    with pytest.raises(RuntimeError, match="configuration error"):
        quality_debt.collect_ruff_findings(tmp_path, runner)


def test_ruff_collector_rejects_failed_empty_output(tmp_path: Path) -> None:
    def runner(command: Any, cwd: Path) -> quality_debt.CommandResult:
        del command, cwd
        return quality_debt.CommandResult(
            returncode=1,
            stdout="[]",
            stderr="",
        )

    with pytest.raises(quality_debt.QualityDebtError, match="no parseable findings"):
        quality_debt.collect_ruff_findings(tmp_path, runner)


def test_mypy_collector_rejects_failed_empty_output(tmp_path: Path) -> None:
    def runner(command: Any, cwd: Path) -> quality_debt.CommandResult:
        del command, cwd
        return quality_debt.CommandResult(
            returncode=1,
            stdout="",
            stderr="",
        )

    with pytest.raises(
        quality_debt.QualityDebtError,
        match="no parseable error findings",
    ):
        quality_debt.collect_mypy_findings(tmp_path, runner)
