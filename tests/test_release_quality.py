from __future__ import annotations

import pathlib
import subprocess

import pytest
from release_routines.lib import artifact_smoke
from release_routines.lib.check_minimum_constraints import validate_minimum_constraints
from release_routines.lib.project_metadata import load_project

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_repository_minimum_constraints_match_runtime_dependencies() -> None:
    project = load_project(REPO_ROOT / "pyproject.toml")
    dependencies = [str(requirement) for requirement in project["dependencies"]]
    constraints = (REPO_ROOT / "constraints" / "py38-min.txt").read_text(encoding="utf-8")

    assert validate_minimum_constraints(dependencies, constraints) == []


def test_minimum_constraint_validation_accepts_equivalent_zero_components() -> None:
    failures = validate_minimum_constraints(
        ["fsspec>=2024.2", "pyarrow>=14,<23"],
        "fsspec==2024.2.0\npyarrow==14.0.0\n",
    )

    assert failures == []


def test_minimum_constraint_validation_reports_missing_duplicate_mismatch_and_extra() -> None:
    failures = validate_minimum_constraints(
        ["alpha>=1.2,<2", "bravo>=3", "charlie>=4"],
        "alpha==1.3\nbravo==3\nbravo==3.0\ndelta==5\n",
    )

    assert "minimum constraint for 'alpha' is '1.3', expected lower bound '1.2'" in failures
    assert "runtime dependency 'bravo' must have exactly one minimum constraint" in "\n".join(
        failures
    )
    assert "minimum constraint is missing for runtime dependency 'charlie'" in failures
    assert "constraint 'delta' is not a direct runtime dependency" in failures


@pytest.mark.parametrize(
    "requirement",
    ["package<2", "package==1.0", "package>=1,>=2"],
)
def test_minimum_constraint_validation_requires_one_inclusive_lower_bound(
    requirement: str,
) -> None:
    failures = validate_minimum_constraints([requirement], "package==1\n")

    assert "must declare exactly one inclusive lower bound" in failures[0]


def test_minimum_constraint_validation_rejects_non_exact_constraint_lines() -> None:
    failures = validate_minimum_constraints(["package>=1"], "package>=1\n")

    assert "must be an exact name==version pin" in failures[0]
    assert "minimum constraint is missing" in failures[1]


def test_select_artifacts_requires_one_wheel_and_one_sdist(tmp_path: pathlib.Path) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    (dist_dir / "package-1.0.tar.gz").touch()
    (dist_dir / "package-1.0-py3-none-any.whl").touch()

    wheel, sdist = artifact_smoke._select_artifacts(dist_dir)

    assert wheel.name == "package-1.0-py3-none-any.whl"
    assert sdist.name == "package-1.0.tar.gz"

    (dist_dir / "unexpected.zip").touch()
    with pytest.raises(artifact_smoke.ArtifactSmokeError, match="unexpected"):
        artifact_smoke._select_artifacts(dist_dir)


def test_copy_project_source_excludes_local_and_generated_state(tmp_path: pathlib.Path) -> None:
    source = tmp_path / "project"
    source.mkdir()
    (source / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (source / ".connections").write_text("secret", encoding="utf-8")
    (source / ".git").mkdir()
    (source / ".git" / "config").write_text("ignored", encoding="utf-8")
    (source / "package.egg-info").mkdir()
    destination = tmp_path / "copy"

    artifact_smoke._copy_project_source(source, destination)

    assert (destination / "pyproject.toml").is_file()
    assert not (destination / ".connections").exists()
    assert not (destination / ".git").exists()
    assert not (destination / "package.egg-info").exists()


def test_run_artifact_smoke_publishes_only_checked_artifacts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    output_dir = tmp_path / "verified"
    commands: list[tuple[str, ...]] = []
    installed: list[str] = []

    def fake_run(
        command: list[str],
        *,
        cwd: pathlib.Path,
        capture_output: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del capture_output
        normalized = tuple(str(part) for part in command)
        commands.append(normalized)
        if normalized[1:3] == ("-m", "build"):
            dist_dir = pathlib.Path(normalized[normalized.index("--outdir") + 1])
            (dist_dir / "package-1.0-py3-none-any.whl").touch()
            (dist_dir / "package-1.0.tar.gz").touch()
        return subprocess.CompletedProcess(normalized, 0, stdout="", stderr="")

    monkeypatch.setattr(artifact_smoke, "_run", fake_run)
    monkeypatch.setattr(
        artifact_smoke,
        "_verify_installed_artifact",
        lambda artifact, workspace: installed.append(artifact.name),
    )

    artifacts = artifact_smoke.run_artifact_smoke(
        project_root=project_root,
        output_dir=output_dir,
    )

    assert artifacts == ["package-1.0-py3-none-any.whl", "package-1.0.tar.gz"]
    assert installed == artifacts
    assert sorted(path.name for path in output_dir.iterdir()) == sorted(artifacts)
    assert any(command[1:3] == ("-m", "twine") for command in commands)
    assert any(command[1:3] == ("-m", "check_wheel_contents") for command in commands)


def test_run_artifact_smoke_rejects_nonempty_output_before_build(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    output_dir = tmp_path / "dist"
    output_dir.mkdir()
    (output_dir / "old.whl").touch()
    called = False

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal called
        called = True
        return subprocess.CompletedProcess([], 0)

    monkeypatch.setattr(artifact_smoke, "_run", fake_run)

    with pytest.raises(artifact_smoke.ArtifactSmokeError, match="must be empty"):
        artifact_smoke.run_artifact_smoke(
            project_root=tmp_path,
            output_dir=output_dir,
        )
    assert called is False


def test_verify_installed_artifact_checks_imports_pip_and_cli(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    commands: list[tuple[str, ...]] = []

    def fake_run(
        command: list[str | pathlib.Path],
        *,
        cwd: pathlib.Path,
        capture_output: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, capture_output
        normalized = tuple(str(part) for part in command)
        commands.append(normalized)
        stdout = ""
        if normalized[-1] == "--help":
            stdout = "usage: analytics-toolkit [-h]"
        elif normalized[-2:] == ("sql", "support-matrix"):
            stdout = "Backend Dialect\ngp postgres"
        return subprocess.CompletedProcess(normalized, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(artifact_smoke, "_run", fake_run)

    artifact_smoke._verify_installed_artifact(tmp_path / "package.whl", tmp_path)

    command_text = "\n".join(" ".join(command) for command in commands)
    assert "-m pip install --no-cache-dir" in command_text
    assert "-m pip check" in command_text
    assert "analytics_toolkit.ab_utils" in command_text
    assert "analytics-toolkit --help" in command_text
    assert "analytics-toolkit sql support-matrix" in command_text
