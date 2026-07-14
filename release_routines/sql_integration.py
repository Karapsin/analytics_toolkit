from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = REPO_ROOT / "integration" / "docker-compose.yml"
ARTIFACTS_DIR = REPO_ROOT / ".integration-artifacts"
PROJECT_NAME = "analytics-toolkit-integration"
X86_ARCHITECTURES = {"amd64", "x86_64"}


def _compose_command(*args: str, include_greenplum: bool) -> list[str]:
    command = [
        "docker",
        "compose",
        "--project-name",
        PROJECT_NAME,
        "--file",
        str(COMPOSE_FILE),
    ]
    if include_greenplum:
        command.extend(["--profile", "gp"])
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


def _write_logs(*, include_greenplum: bool) -> None:
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = ARTIFACTS_DIR / "compose.log"
    with log_path.open("w", encoding="utf-8") as stream:
        subprocess.run(
            _compose_command("logs", "--no-color", include_greenplum=include_greenplum),
            cwd=REPO_ROOT,
            check=False,
            stdout=stream,
            stderr=subprocess.STDOUT,
            text=True,
        )


def run(*, include_greenplum: bool) -> int:
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = ARTIFACTS_DIR / "compose.log"
    if log_path.exists():
        log_path.unlink()

    up_command = _compose_command(
        "up",
        "--detach",
        "--wait",
        "--wait-timeout",
        "420",
        include_greenplum=include_greenplum,
    )
    test_env = os.environ.copy()
    test_env.update(
        {
            "ANALYTICS_TOOLKIT_RUN_INTEGRATION": "1",
            "SQL_INTEGRATION_GP": "1" if include_greenplum else "0",
            "AWS_ACCESS_KEY_ID": "integration",
            "AWS_SECRET_ACCESS_KEY": "integration-secret",
            "AWS_DEFAULT_REGION": "us-east-1",
            "AWS_ENDPOINT_URL": "http://127.0.0.1:19001",
            "AWS_ENDPOINT_URL_S3": "http://127.0.0.1:19001",
        }
    )

    result = 1
    started = False
    try:
        result = _run(up_command)
        started = result == 0
        if not started:
            _write_logs(include_greenplum=include_greenplum)
            return result

        result = _run(
            [sys.executable, "-m", "pytest", "-q", "-m", "integration", "tests/integration"],
            env=test_env,
        )
        if result != 0:
            _write_logs(include_greenplum=include_greenplum)
        return result
    finally:
        down_result = _run(
            _compose_command(
                "down",
                "--volumes",
                "--remove-orphans",
                include_greenplum=include_greenplum,
            )
        )
        if started and result == 0 and down_result != 0:
            raise SystemExit(down_result)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run disposable SQL integration tests.")
    parser.add_argument(
        "--with-greenplum",
        action="store_true",
        help="enable the x86_64-only Greenplum profile",
    )
    args = parser.parse_args(argv)
    architecture = platform.machine().lower()
    if args.with_greenplum and architecture not in X86_ARCHITECTURES:
        parser.error("--with-greenplum requires an x86_64 host")
    return run(include_greenplum=architecture in X86_ARCHITECTURES)


if __name__ == "__main__":
    raise SystemExit(main())
