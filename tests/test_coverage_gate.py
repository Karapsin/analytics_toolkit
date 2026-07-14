from __future__ import annotations

import json
from pathlib import Path

import pytest
from release_routines.lib import check_coverage


def _file(
    statements: int,
    missing_lines: int,
    branches: int = 0,
    missing_branches: int = 0,
) -> dict[str, object]:
    return {
        "summary": {
            "num_statements": statements,
            "missing_lines": missing_lines,
            "num_branches": branches,
            "missing_branches": missing_branches,
        }
    }


def _write(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_gate_accepts_exact_boundaries_and_zero_branch_modules(tmp_path: Path) -> None:
    report = _write(
        tmp_path / "coverage.json",
        {
            "files": {
                "analytics_toolkit/ab_utils/api.py": _file(10, 1, 10, 1),
                "analytics_toolkit/ab_utils/constants.py": _file(10, 1),
            }
        },
    )
    targets = _write(
        tmp_path / "targets.json",
        {
            "overall": {"statements": 90, "branches": 90, "combined": 90},
            "prefixes": {
                "ab_utils/constants": {"branches": 100},
                "ab_utils": {"combined": 90},
            },
        },
    )

    assert check_coverage.main([str(report), "--targets", str(targets)]) == 0


def test_gate_reports_each_failed_metric_and_prefix(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = _write(
        tmp_path / "coverage.json",
        {"files": {"analytics_toolkit/sql/read.py": _file(10, 2, 4, 2)}},
    )
    targets = _write(
        tmp_path / "targets.json",
        {
            "overall": {"statements": 81, "branches": 51, "combined": 75},
            "prefixes": {"sql": {"combined": 80}},
        },
    )

    assert check_coverage.main([str(report), "--targets", str(targets)]) == 1
    output = capsys.readouterr().out
    assert "overall statements: 80.00% target=81.00% covered=8/10 missing=2" in output
    assert "overall branches: 50.00% target=51.00% covered=2/4 missing=2" in output
    assert "sql combined: 71.43% target=80.00% covered=10/14 missing=4" in output


def test_prefix_aggregation_does_not_match_sibling_names() -> None:
    files = {
        "analytics_toolkit/sql/a.py": check_coverage.OpportunityCounts(8, 10, 1, 2),
        "analytics_toolkit/sql/nested/b.py": check_coverage.OpportunityCounts(4, 5, 2, 3),
        "analytics_toolkit/sql_format.py": check_coverage.OpportunityCounts(0, 10, 0, 2),
    }

    assert check_coverage.aggregate_prefix(files, "sql") == check_coverage.OpportunityCounts(
        12, 15, 3, 5
    )


def test_empty_prefix_is_rejected() -> None:
    with pytest.raises(check_coverage.CoverageGateError, match="matched no files"):
        check_coverage.aggregate_prefix(
            {"analytics_toolkit/sql/a.py": check_coverage.OpportunityCounts()}, "excel"
        )


def test_unconfigured_optional_prefix_does_not_require_matching_files(
    tmp_path: Path,
) -> None:
    report = _write(
        tmp_path / "coverage.json",
        {"files": {"analytics_toolkit/sql/a.py": _file(1, 0)}},
    )
    targets = _write(
        tmp_path / "targets.json",
        {
            "overall": {"statements": 100, "branches": 100, "combined": 100},
            "prefixes": {},
        },
    )

    assert check_coverage.main([str(report), "--targets", str(targets)]) == 0


def test_configured_required_prefix_must_match_before_targets_are_updated(
    tmp_path: Path,
) -> None:
    report = _write(
        tmp_path / "coverage.json",
        {"files": {"analytics_toolkit/sql/a.py": _file(1, 0)}},
    )
    targets = _write(
        tmp_path / "targets.json",
        {
            "overall": {"statements": 0, "branches": 0, "combined": 0},
            "prefixes": {"excel": {"combined": 0}},
        },
    )
    original = targets.read_text(encoding="utf-8")

    with pytest.raises(SystemExit) as error:
        check_coverage.main([str(report), "--targets", str(targets), "--update-targets"])

    assert error.value.code == 2
    assert targets.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"files": {}},
        {"files": {"module.py": {}}},
        {"files": {"module.py": _file(1, 2)}},
        {"files": {"module.py": _file(1, 0, 0, 1)}},
    ],
)
def test_malformed_reports_are_rejected(tmp_path: Path, payload: object) -> None:
    report = _write(tmp_path / "coverage.json", payload)

    with pytest.raises(check_coverage.CoverageGateError):
        check_coverage.load_report(report)


def test_gate_ratchets_targets_downward_rounded_and_requires_rerun(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = _write(
        tmp_path / "coverage.json",
        {"files": {"analytics_toolkit/sql/a.py": _file(3, 0, 4, 1)}},
    )
    targets = _write(
        tmp_path / "targets.json",
        {
            "overall": {"statements": 90, "branches": 70, "combined": 80},
            "prefixes": {"sql": {"combined": 80}},
        },
    )

    assert (
        check_coverage.main(
            [
                str(report),
                "--targets",
                str(targets),
                "--update-targets",
            ]
        )
        == 1
    )
    updated = json.loads(targets.read_text(encoding="utf-8"))
    assert updated["overall"] == {
        "statements": 100.0,
        "branches": 75.0,
        "combined": 85.71,
    }
    assert updated["prefixes"]["sql"] == {"combined": 85.71}
    output = capsys.readouterr().out
    assert "Coverage targets raised; review and rerun" in output
    assert "sql combined: 80.00% -> 85.71% covered=6/7 missing=1 prefix=sql" in output

    assert (
        check_coverage.main(
            [
                str(report),
                "--targets",
                str(targets),
                "--update-targets",
            ]
        )
        == 0
    )


def test_gate_does_not_lower_or_rewrite_targets_after_regression(
    tmp_path: Path,
) -> None:
    report = _write(
        tmp_path / "coverage.json",
        {"files": {"analytics_toolkit/sql/a.py": _file(10, 2, 4, 2)}},
    )
    targets = _write(
        tmp_path / "targets.json",
        {
            "overall": {"statements": 90, "branches": 60, "combined": 80},
            "prefixes": {},
        },
    )
    original = targets.read_text(encoding="utf-8")

    assert (
        check_coverage.main(
            [
                str(report),
                "--targets",
                str(targets),
                "--update-targets",
            ]
        )
        == 1
    )
    assert targets.read_text(encoding="utf-8") == original


def test_ratchet_replaces_targets_atomically_with_stable_formatting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _write(
        tmp_path / "coverage.json",
        {"files": {"analytics_toolkit/sql/a.py": _file(3, 0, 4, 1)}},
    )
    targets = _write(
        tmp_path / "targets.json",
        {
            "prefixes": {"sql": {"combined": 80}},
            "overall": {"statements": 90, "branches": 70, "combined": 80},
        },
    )
    replacements: list[tuple[Path, Path]] = []
    real_replace = Path.replace
    original_mode = targets.stat().st_mode

    def tracked_replace(source: Path, target: str | Path) -> Path:
        source_path = source
        target_path = Path(target)
        replacements.append((source_path, target_path))
        assert source_path.parent == target_path.parent
        assert source_path != target_path
        return real_replace(source, target)

    monkeypatch.setattr(Path, "replace", tracked_replace)

    assert check_coverage.main([str(report), "--targets", str(targets), "--update-targets"]) == 1
    assert len(replacements) == 1
    assert replacements[0][1] == targets
    assert not replacements[0][0].exists()
    assert targets.stat().st_mode == original_mode
    assert targets.read_text(encoding="utf-8") == (
        "{\n"
        '  "overall": {\n'
        '    "branches": 75.0,\n'
        '    "combined": 85.71,\n'
        '    "statements": 100.0\n'
        "  },\n"
        '  "prefixes": {\n'
        '    "sql": {\n'
        '      "combined": 85.71\n'
        "    }\n"
        "  }\n"
        "}\n"
    )


def test_atomic_replace_failure_preserves_committed_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _write(
        tmp_path / "coverage.json",
        {"files": {"analytics_toolkit/sql/a.py": _file(1, 0)}},
    )
    targets = _write(
        tmp_path / "targets.json",
        {
            "overall": {"statements": 0, "branches": 0, "combined": 0},
            "prefixes": {},
        },
    )
    original = targets.read_text(encoding="utf-8")

    def fail_replace(*_args: object) -> None:
        message = "injected replace failure"
        raise OSError(message)

    monkeypatch.setattr(Path, "replace", fail_replace)

    with pytest.raises(SystemExit) as error:
        check_coverage.main([str(report), "--targets", str(targets), "--update-targets"])

    assert error.value.code == 2
    assert targets.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob(".targets.json.*.tmp")) == []


def test_check_only_mode_never_rewrites_targets(tmp_path: Path) -> None:
    report = _write(
        tmp_path / "coverage.json",
        {"files": {"analytics_toolkit/sql/a.py": _file(1, 0)}},
    )
    targets = _write(
        tmp_path / "targets.json",
        {
            "overall": {"statements": 0, "branches": 0, "combined": 0},
            "prefixes": {},
        },
    )
    original = targets.read_text(encoding="utf-8")

    assert check_coverage.main([str(report), "--targets", str(targets), "--check-only"]) == 0
    assert targets.read_text(encoding="utf-8") == original
