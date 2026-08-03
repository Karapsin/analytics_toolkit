from __future__ import annotations

import argparse
import pathlib
import shutil
import subprocess
import sys
import tempfile
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence


PUBLIC_MODULES = (
    "analytics_toolkit",
    "analytics_toolkit.ab_utils",
    "analytics_toolkit.dates",
    "analytics_toolkit.datetime",
    "analytics_toolkit.excel",
    "analytics_toolkit.general",
    "analytics_toolkit.sql",
    "analytics_toolkit.sql_format",
)
_IGNORED_SOURCE_NAMES = {
    ".connections",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".rag_index",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "htmlcov",
}


class ArtifactSmokeError(RuntimeError):
    """Raised when a built distribution fails a smoke check."""


def _run(
    command: Sequence[str],
    *,
    cwd: pathlib.Path,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(part) for part in command],
        cwd=cwd,
        check=True,
        text=True,
        capture_output=capture_output,
    )


def _copy_project_source(project_root: pathlib.Path, destination: pathlib.Path) -> None:
    def ignore_names(_directory: str, names: list[str]) -> set[str]:
        return {
            name
            for name in names
            if name in _IGNORED_SOURCE_NAMES or name.endswith((".egg-info", ".pyc"))
        }

    shutil.copytree(project_root, destination, ignore=ignore_names)


def _select_artifacts(dist_dir: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    wheels = sorted(dist_dir.glob("*.whl"))
    sdists = sorted(dist_dir.glob("*.tar.gz"))
    unexpected = sorted(
        path.name
        for path in dist_dir.iterdir()
        if path.is_file() and path not in {*wheels, *sdists}
    )
    if len(wheels) != 1 or len(sdists) != 1 or unexpected:
        message = (
            "expected exactly one wheel and one .tar.gz sdist; "
            f"found wheels={[path.name for path in wheels]!r}, "
            f"sdists={[path.name for path in sdists]!r}, unexpected={unexpected!r}"
        )
        raise ArtifactSmokeError(message)
    return wheels[0], sdists[0]


def _venv_executable(venv_dir: pathlib.Path, name: str) -> pathlib.Path:
    if sys.platform == "win32":
        return venv_dir / "Scripts" / f"{name}.exe"
    return venv_dir / "bin" / name


def _verify_installed_artifact(artifact: pathlib.Path, workspace: pathlib.Path) -> None:
    install_root = pathlib.Path(tempfile.mkdtemp(prefix="install-", dir=workspace))
    venv_dir = install_root / "venv"
    _run([sys.executable, "-m", "venv", str(venv_dir)], cwd=install_root)
    python = _venv_executable(venv_dir, "python")
    _run(
        [python, "-m", "pip", "install", "--no-cache-dir", str(artifact)],
        cwd=install_root,
    )
    _run([python, "-m", "pip", "check"], cwd=install_root)
    _run(
        [
            python,
            "-m",
            "pip",
            "install",
            "--no-cache-dir",
            f"{artifact}[clickhouse-native]",
        ],
        cwd=install_root,
    )
    _run([python, "-c", "import clickhouse_driver"], cwd=install_root)

    module_literals = ", ".join(repr(module) for module in PUBLIC_MODULES)
    import_script = f"""
import importlib
import pathlib
import sysconfig

modules = ({module_literals},)
for module in modules:
    importlib.import_module(module)
package = importlib.import_module("analytics_toolkit")
package_path = pathlib.Path(package.__file__).resolve()
site_packages = pathlib.Path(sysconfig.get_paths()["purelib"]).resolve()
try:
    package_path.relative_to(site_packages)
except ValueError as exc:
    raise SystemExit(
        f"analytics_toolkit imported outside the isolated site-packages: {{package_path}}"
    ) from exc
print(f"Imported all public modules from {{package_path}}")
"""
    _run([python, "-c", import_script], cwd=install_root)

    cli = _venv_executable(venv_dir, "analytics-toolkit")
    help_result = _run([cli, "--help"], cwd=install_root, capture_output=True)
    if "usage: analytics-toolkit" not in help_result.stdout:
        message = "installed CLI help output is missing its usage line"
        raise ArtifactSmokeError(message)
    support_result = _run(
        [cli, "sql", "support-matrix"],
        cwd=install_root,
        capture_output=True,
    )
    if "Backend" not in support_result.stdout or "gp" not in support_result.stdout:
        message = "installed CLI support matrix output is incomplete"
        raise ArtifactSmokeError(message)


def _publish_verified_artifacts(
    artifacts: Sequence[pathlib.Path], output_dir: pathlib.Path
) -> list[pathlib.Path]:
    if output_dir.exists() and any(output_dir.iterdir()):
        message = f"artifact output directory must be empty: {output_dir}"
        raise ArtifactSmokeError(message)
    output_dir.mkdir(parents=True, exist_ok=True)
    published: list[pathlib.Path] = []
    for artifact in artifacts:
        destination = output_dir / artifact.name
        shutil.copy2(artifact, destination)
        published.append(destination)
    return published


def run_artifact_smoke(
    *,
    project_root: pathlib.Path,
    output_dir: pathlib.Path | None = None,
) -> list[str]:
    project_root = project_root.resolve()
    if output_dir is not None:
        output_dir = output_dir.resolve()
        if output_dir.exists() and any(output_dir.iterdir()):
            message = f"artifact output directory must be empty: {output_dir}"
            raise ArtifactSmokeError(message)

    with tempfile.TemporaryDirectory(prefix="analytics-toolkit-artifacts-") as temp_name:
        workspace = pathlib.Path(temp_name)
        source_dir = workspace / "source"
        dist_dir = workspace / "dist"
        _copy_project_source(project_root, source_dir)
        dist_dir.mkdir()

        _run(
            [sys.executable, "-m", "build", "--outdir", str(dist_dir), str(source_dir)],
            cwd=workspace,
        )
        wheel, sdist = _select_artifacts(dist_dir)
        _run([sys.executable, "-m", "twine", "check", str(wheel), str(sdist)], cwd=workspace)
        _run([sys.executable, "-m", "check_wheel_contents", str(wheel)], cwd=workspace)
        for artifact in (wheel, sdist):
            _verify_installed_artifact(artifact, workspace)

        artifact_names = [wheel.name, sdist.name]
        if output_dir is not None:
            _publish_verified_artifacts((wheel, sdist), output_dir)
        return artifact_names


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Build and smoke-test wheel and sdist artifacts outside the checkout."
    )
    parser.add_argument("--project-root", type=pathlib.Path, default=pathlib.Path())
    parser.add_argument("--output-dir", type=pathlib.Path)
    args = parser.parse_args(argv)

    artifacts = run_artifact_smoke(
        project_root=args.project_root,
        output_dir=args.output_dir,
    )
    print(f"Verified wheel and sdist artifacts: {', '.join(artifacts)}")


if __name__ == "__main__":
    main()
