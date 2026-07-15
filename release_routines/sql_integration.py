from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
INTEGRATION_DIR = REPO_ROOT / "integration"
CORE_COMPOSE_FILE = INTEGRATION_DIR / "docker-compose.yml"
AUTH_COMPOSE_FILE = INTEGRATION_DIR / "docker-compose.auth.yml"
ARTIFACTS_DIR = REPO_ROOT / ".integration-artifacts"
PROJECT_NAME = "analytics-toolkit-integration"
X86_ARCHITECTURES = {"amd64", "x86_64"}
PROFILES = ("core", "auth", "all", "fault")


def _compose_command(
    *args: str,
    include_greenplum: bool,
    profile: str,
) -> list[str]:
    command = [
        "docker",
        "compose",
        "--project-name",
        f"{PROJECT_NAME}-{profile}",
        "--file",
        str(CORE_COMPOSE_FILE),
    ]
    if profile == "auth":
        command.extend(["--file", str(AUTH_COMPOSE_FILE)])
    if include_greenplum:
        command.extend(["--profile", "gp"])
    if profile == "auth":
        command.extend(["--profile", "auth"])
    command.extend(args)
    return command


def _run(command: Sequence[str], *, env: dict[str, str] | None = None) -> int:
    completed = subprocess.run(
        list(command),
        cwd=REPO_ROOT,
        check=False,
        env=env,
    )
    return completed.returncode


def _capture(
    path: Path,
    command: Sequence[str],
    *,
    append: bool = False,
) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a" if append else "w", encoding="utf-8") as stream:
        completed = subprocess.run(
            list(command),
            cwd=REPO_ROOT,
            check=False,
            stdout=stream,
            stderr=subprocess.STDOUT,
            text=True,
        )
    return completed.returncode


def _write_diagnostics(*, include_greenplum: bool, profile: str) -> None:
    profile_dir = ARTIFACTS_DIR / profile
    _capture(
        profile_dir / "compose.log",
        _compose_command(
            "logs",
            "--no-color",
            include_greenplum=include_greenplum,
            profile=profile,
        ),
    )
    _capture(
        profile_dir / "service-health.txt",
        _compose_command(
            "ps",
            "--all",
            "--format",
            "json",
            include_greenplum=include_greenplum,
            profile=profile,
        ),
    )


def _test_environment(*, include_greenplum: bool, profile: str) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "ANALYTICS_TOOLKIT_RUN_INTEGRATION": "1",
            "SQL_INTEGRATION_PROFILE": profile,
            "SQL_INTEGRATION_GP": "1" if include_greenplum else "0",
            "SQL_INTEGRATION_CERTS": str(ARTIFACTS_DIR / "auth" / "certs"),
            "AWS_ACCESS_KEY_ID": "integration",
            "AWS_SECRET_ACCESS_KEY": "integration-secret",
            "AWS_DEFAULT_REGION": "us-east-1",
            "AWS_ENDPOINT_URL": "http://127.0.0.1:19001",
            "AWS_ENDPOINT_URL_S3": "http://127.0.0.1:19001",
        }
    )
    return environment


def _pytest_marker(profile: str) -> str:
    if profile == "core":
        return "integration and integration_core"
    if profile == "auth":
        return "integration and integration_auth"
    return "integration and integration_fault"


def _assert_teardown_clean(*, profile: str) -> int:
    containers = subprocess.run(
        [
            "docker",
            "ps",
            "-aq",
            "--filter",
            f"label=com.docker.compose.project={PROJECT_NAME}-{profile}",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    volumes = subprocess.run(
        [
            "docker",
            "volume",
            "ls",
            "-q",
            "--filter",
            f"label=com.docker.compose.project={PROJECT_NAME}-{profile}",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    leaked = {
        "containers": containers.stdout.split(),
        "volumes": volumes.stdout.split(),
    }
    artifact = ARTIFACTS_DIR / profile / "runner-leaks.json"
    artifact.write_text(json.dumps(leaked, indent=2), encoding="utf-8")
    return 1 if leaked["containers"] or leaked["volumes"] else 0


def run_profile(*, profile: str, include_greenplum: bool) -> int:
    profile_dir = ARTIFACTS_DIR / profile
    profile_dir.mkdir(parents=True, exist_ok=True)
    up_command = _compose_command(
        "up",
        "--detach",
        "--wait",
        "--wait-timeout",
        "420",
        include_greenplum=include_greenplum,
        profile=profile,
    )
    result = 1
    started = False
    try:
        result = _run(up_command)
        started = result == 0
        _write_diagnostics(include_greenplum=include_greenplum, profile=profile)
        if not started:
            return result
        if profile == "auth":
            cert_dir = profile_dir / "certs"
            cert_dir.mkdir(parents=True, exist_ok=True)
            for filename in ("ca.crt", "client.crt", "client.key"):
                copy_result = _run(
                    _compose_command(
                        "cp",
                        f"auth-proxy:/certs/{filename}",
                        str(cert_dir / filename),
                        include_greenplum=include_greenplum,
                        profile=profile,
                    )
                )
                if copy_result != 0:
                    return copy_result
            (cert_dir / "client.key").chmod(0o600)
        result = _run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-m",
                _pytest_marker(profile),
                "--junitxml",
                str(profile_dir / "pytest.xml"),
                "tests/integration",
            ],
            env=_test_environment(
                include_greenplum=include_greenplum,
                profile=profile,
            ),
        )
        _write_diagnostics(include_greenplum=include_greenplum, profile=profile)
        return result
    finally:
        down_result = _run(
            _compose_command(
                "down",
                "--volumes",
                "--remove-orphans",
                include_greenplum=include_greenplum,
                profile=profile,
            )
        )
        leak_result = _assert_teardown_clean(profile=profile)
        if started and result == 0 and (down_result != 0 or leak_result != 0):
            raise SystemExit(down_result or leak_result)


def run(*, profile: str, include_greenplum: bool) -> int:
    selected = ("core", "auth") if profile == "all" else (profile,)
    for selected_profile in selected:
        result = run_profile(
            profile=selected_profile,
            include_greenplum=include_greenplum,
        )
        if result != 0:
            return result
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run disposable SQL integration tests.")
    parser.add_argument("--profile", choices=PROFILES, default="all")
    parser.add_argument(
        "--with-greenplum",
        action="store_true",
        help="require the x86_64-only Greenplum profile",
    )
    args = parser.parse_args(argv)
    architecture = platform.machine().lower()
    if args.with_greenplum and architecture not in X86_ARCHITECTURES:
        parser.error("--with-greenplum requires an x86_64 host")
    return run(
        profile=args.profile,
        include_greenplum=architecture in X86_ARCHITECTURES,
    )


if __name__ == "__main__":
    raise SystemExit(main())
