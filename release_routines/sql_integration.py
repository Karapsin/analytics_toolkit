from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Sequence

import psycopg2

REPO_ROOT = Path(__file__).resolve().parents[1]
INTEGRATION_DIR = REPO_ROOT / "integration"
CORE_COMPOSE_FILE = INTEGRATION_DIR / "docker-compose.yml"
AUTH_COMPOSE_FILE = INTEGRATION_DIR / "docker-compose.auth.yml"
ARTIFACTS_DIR = REPO_ROOT / ".integration-artifacts"
PROJECT_NAME = "analytics-toolkit-integration"
TEST_TIMEOUT_SECONDS = 300
X86_ARCHITECTURES = {"amd64", "x86_64"}
PROFILES = ("core", "auth", "all", "fault", "stress")
FAULT_GROUPS = ("database", "staging", "authentication")
CLICKHOUSE_DRIVERS = ("http", "native", "both")
GREENPLUM_TLS_STABLE_SUCCESSES = 3


def _compose_command(
    *args: str,
    include_greenplum: bool,
    profile: str,
    clickhouse_driver: str,
) -> list[str]:
    command = [
        "docker",
        "compose",
        "--project-name",
        f"{PROJECT_NAME}-{profile}-{clickhouse_driver}",
        "--file",
        str(CORE_COMPOSE_FILE),
    ]
    uses_auth = profile in {"auth", "fault"}
    if uses_auth:
        command.extend(["--file", str(AUTH_COMPOSE_FILE)])
    if include_greenplum:
        command.extend(["--profile", "gp"])
    if uses_auth:
        command.extend(["--profile", "auth"])
    if profile == "stress":
        command.extend(["--profile", "stress"])
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


def _write_diagnostics(  # noqa: C901 - gathers independent best-effort artifacts.
    *, include_greenplum: bool, profile: str, clickhouse_driver: str
) -> None:
    profile_dir = ARTIFACTS_DIR / profile / clickhouse_driver
    _capture(
        profile_dir / "compose.log",
        _compose_command(
            "logs",
            "--no-color",
            include_greenplum=include_greenplum,
            profile=profile,
            clickhouse_driver=clickhouse_driver,
        ),
    )
    health = subprocess.run(
        _compose_command(
            "ps",
            "--all",
            "--format",
            "json",
            include_greenplum=include_greenplum,
            profile=profile,
            clickhouse_driver=clickhouse_driver,
        ),
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    records: list[object] = []
    for line in health.stdout.splitlines():
        if line.strip():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                records.append({"unparsed": line})
    (profile_dir / "service-health.json").write_text(
        json.dumps(records, indent=2) + "\n",
        encoding="utf-8",
    )
    for filename in (
        "leaks.json",
        "minio-objects.json",
        "active-queries.json",
        "failed-query-details.json",
        "operation-retry-timeline.json",
        "connection-identities.json",
        "orchestration-timeline.json",
        "type-normalization-mismatch.json",
        "memory-profile.json",
        "connection-pressure.json",
        "lock-timeline.json",
        "concurrent-writer-results.json",
    ):
        path = profile_dir / filename
        if not path.exists():
            path.write_text("[]\n", encoding="utf-8")
    if profile == "fault":
        timeline = profile_dir / "fault-timeline.json"
        if not timeline.exists():
            timeline.write_text("[]\n", encoding="utf-8")
    if profile in {"auth", "fault"}:
        for filename in ("oauth-browser.log", "authentication.log"):
            path = profile_dir / filename
            if not path.exists():
                path.write_text("", encoding="utf-8")


def _test_environment(
    *,
    include_greenplum: bool,
    profile: str,
    run_id: str,
    fault_group: str | None,
    clickhouse_driver: str,
) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "ANALYTICS_TOOLKIT_RUN_INTEGRATION": "1",
            "SQL_INTEGRATION_PROFILE": profile,
            "SQL_INTEGRATION_RUN_ID": run_id,
            "SQL_INTEGRATION_ARTIFACT_DIR": str(ARTIFACTS_DIR / profile / clickhouse_driver),
            "SQL_INTEGRATION_COMPOSE_PROJECT": (f"{PROJECT_NAME}-{profile}-{clickhouse_driver}"),
            "SQL_INTEGRATION_CLICKHOUSE_DRIVER": clickhouse_driver,
            "SQL_INTEGRATION_GP": "1" if include_greenplum else "0",
            "SQL_INTEGRATION_CERTS": str(ARTIFACTS_DIR / profile / clickhouse_driver / "certs"),
            "AWS_ACCESS_KEY_ID": "integration",
            "AWS_SECRET_ACCESS_KEY": "integration-secret",
            "AWS_DEFAULT_REGION": "us-east-1",
            "AWS_ENDPOINT_URL": "http://127.0.0.1:19001",
            "AWS_ENDPOINT_URL_S3": "http://127.0.0.1:19001",
        }
    )
    if fault_group:
        environment["SQL_INTEGRATION_FAULT_GROUP"] = fault_group
    return environment


def _pytest_marker(profile: str) -> str:
    if profile == "core":
        return "integration and integration_core"
    if profile == "auth":
        return "integration and integration_auth"
    if profile == "stress":
        return "integration and integration_stress"
    return "integration and integration_fault"


def _pytest_command(profile: str, profile_dir: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "--maxfail=1",
        f"--timeout={TEST_TIMEOUT_SECONDS}",
        "--timeout-method=signal",
        "-m",
        _pytest_marker(profile),
        "--junitxml",
        str(profile_dir / "pytest.xml"),
        "tests/integration",
    ]


def _assert_no_manifest_skips(
    *, profile: str, include_greenplum: bool, clickhouse_driver: str
) -> int:
    if not include_greenplum or profile not in {"core", "auth"}:
        return 0
    report = ARTIFACTS_DIR / profile / clickhouse_driver / "pytest.xml"
    try:
        root = ET.parse(report).getroot()  # noqa: S314 - parses our local pytest report.
    except (OSError, ET.ParseError):
        return 1
    skipped = sum(int(suite.attrib.get("skipped", "0")) for suite in root.iter("testsuite"))
    return 1 if skipped else 0


def _assert_teardown_clean(*, profile: str, clickhouse_driver: str) -> int:
    project_name = f"{PROJECT_NAME}-{profile}-{clickhouse_driver}"
    containers = subprocess.run(
        [
            "docker",
            "ps",
            "-aq",
            "--filter",
            f"label=com.docker.compose.project={project_name}",
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
            f"label=com.docker.compose.project={project_name}",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    networks = subprocess.run(
        [
            "docker",
            "network",
            "ls",
            "-q",
            "--filter",
            f"label=com.docker.compose.project={project_name}",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    leaked = {
        "containers": containers.stdout.split(),
        "volumes": volumes.stdout.split(),
        "networks": networks.stdout.split(),
    }
    artifact = ARTIFACTS_DIR / profile / clickhouse_driver / "runner-leaks.json"
    artifact.write_text(json.dumps(leaked, indent=2), encoding="utf-8")
    return 1 if any(leaked.values()) else 0


def _assert_transport_scenario_parity(*, profile: str) -> int:
    manifests: dict[str, list[object]] = {}
    for driver in ("http", "native"):
        path = ARTIFACTS_DIR / profile / driver / "collected-scenarios.json"
        try:
            manifests[driver] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifests[driver] = []
    matches = bool(manifests["http"]) and manifests["http"] == manifests["native"]
    report = {
        "profile": profile,
        "matches": matches,
        "http_scenarios": len(manifests["http"]),
        "native_scenarios": len(manifests["native"]),
    }
    (ARTIFACTS_DIR / profile / "transport-parity.json").write_text(
        json.dumps(report, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0 if matches else 1


def _wait_for_greenplum_tls(cert_dir: Path, *, timeout_seconds: float = 60.0) -> int:
    deadline = time.monotonic() + timeout_seconds
    consecutive_successes = 0
    last_error: Exception | None = None
    attempts: list[dict[str, object]] = []
    while time.monotonic() < deadline:
        try:
            connection = psycopg2.connect(
                host="localhost",
                port=int(os.environ.get("SQL_INTEGRATION_GP_TLS_PORT", "19432")),
                dbname="analytics_toolkit",
                user="gpadmin",
                password=os.environ.get("SQL_INTEGRATION_GREENPLUM_PASSWORD", "integration"),
                sslmode="verify-full",
                sslrootcert=str(cert_dir / "ca.crt"),
                sslcert=str(cert_dir / "client.crt"),
                sslkey=str(cert_dir / "client.key"),
                connect_timeout=3,
            )
            connection.close()
            consecutive_successes += 1
            attempts.append({"connected": True, "consecutive_successes": consecutive_successes})
            if consecutive_successes == GREENPLUM_TLS_STABLE_SUCCESSES:
                (cert_dir.parent / "greenplum-tls-readiness.json").write_text(
                    json.dumps({"ready": True, "attempts": attempts}, indent=2) + "\n",
                    encoding="utf-8",
                )
                return 0
        except Exception as error:  # noqa: BLE001 - readiness retries transient drivers.
            last_error = error
            consecutive_successes = 0
            attempts.append({"connected": False, "error": repr(error)})
        time.sleep(1)
    (cert_dir.parent / "greenplum-tls-readiness.json").write_text(
        json.dumps({"ready": False, "attempts": attempts}, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"Greenplum mTLS route did not become stable: {last_error!r}",
        file=sys.stderr,
    )
    return 1


def run_profile(
    *,
    profile: str,
    include_greenplum: bool,
    fault_group: str | None = None,
    clickhouse_driver: str,
) -> int:
    if clickhouse_driver == "native" and importlib.util.find_spec("clickhouse_driver") is None:
        print(
            "Native ClickHouse integration requires the clickhouse-native extra: "
            "pip install -e '.[clickhouse-native]'",
            file=sys.stderr,
        )
        return 2
    profile_dir = ARTIFACTS_DIR / profile / clickhouse_driver
    profile_dir.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex[:12]
    up_command = _compose_command(
        "up",
        "--detach",
        "--wait",
        "--wait-timeout",
        "420",
        include_greenplum=include_greenplum,
        profile=profile,
        clickhouse_driver=clickhouse_driver,
    )
    result = 1
    started = False
    try:
        result = _capture(profile_dir / "startup.log", up_command)
        started = result == 0
        _write_diagnostics(
            include_greenplum=include_greenplum,
            profile=profile,
            clickhouse_driver=clickhouse_driver,
        )
        if not started:
            return result
        if profile in {"auth", "fault"}:
            cert_dir = profile_dir / "certs"
            cert_dir.mkdir(parents=True, exist_ok=True)
            for filename in (
                "ca.crt",
                "wrong-ca.crt",
                "server.crt",
                "client.crt",
                "client.key",
                "invalid-client.crt",
                "invalid-client.key",
                "dns-server.crt",
            ):
                copy_result = _capture(
                    cert_dir / filename,
                    _compose_command(
                        "exec",
                        "--user",
                        "root",
                        "-T",
                        "auth-proxy",
                        "cat",
                        f"/certs/{filename}",
                        include_greenplum=include_greenplum,
                        profile=profile,
                        clickhouse_driver=clickhouse_driver,
                    ),
                )
                if copy_result != 0:
                    return copy_result
            (cert_dir / "client.key").chmod(0o600)
            ca_text = (cert_dir / "ca.crt").read_text(encoding="utf-8")
            (cert_dir / "ca-copy.crt").write_text(ca_text, encoding="utf-8")
            (cert_dir / "ca-bundle.crt").write_text(ca_text + ca_text, encoding="utf-8")
            if include_greenplum and _wait_for_greenplum_tls(cert_dir) != 0:
                return 1
        result = _run(
            _pytest_command(profile, profile_dir),
            env=_test_environment(
                include_greenplum=include_greenplum,
                profile=profile,
                run_id=run_id,
                fault_group=fault_group,
                clickhouse_driver=clickhouse_driver,
            ),
        )
        if result == 0:
            result = _assert_no_manifest_skips(
                profile=profile,
                include_greenplum=include_greenplum,
                clickhouse_driver=clickhouse_driver,
            )
        _write_diagnostics(
            include_greenplum=include_greenplum,
            profile=profile,
            clickhouse_driver=clickhouse_driver,
        )
        return result
    finally:
        down_result = _run(
            _compose_command(
                "down",
                "--volumes",
                "--remove-orphans",
                include_greenplum=include_greenplum,
                profile=profile,
                clickhouse_driver=clickhouse_driver,
            )
        )
        leak_result = _assert_teardown_clean(
            profile=profile,
            clickhouse_driver=clickhouse_driver,
        )
        if started and result == 0 and (down_result != 0 or leak_result != 0):
            raise SystemExit(down_result or leak_result)


def run(
    *,
    profile: str,
    include_greenplum: bool,
    fault_group: str | None = None,
    clickhouse_driver: str = "both",
) -> int:
    selected = ("core", "auth", "fault", "stress") if profile == "all" else (profile,)
    drivers = ("http", "native") if clickhouse_driver == "both" else (clickhouse_driver,)
    for selected_profile in selected:
        for selected_driver in drivers:
            result = run_profile(
                profile=selected_profile,
                include_greenplum=include_greenplum,
                fault_group=fault_group,
                clickhouse_driver=selected_driver,
            )
            if result != 0:
                return result
        if clickhouse_driver == "both":
            parity_result = _assert_transport_scenario_parity(profile=selected_profile)
            if parity_result != 0:
                return parity_result
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run disposable SQL integration tests.")
    parser.add_argument("--profile", choices=PROFILES, default="all")
    parser.add_argument("--fault-group", choices=FAULT_GROUPS)
    parser.add_argument(
        "--clickhouse-driver",
        choices=CLICKHOUSE_DRIVERS,
        default="both",
        help="ClickHouse transport to validate; 'both' runs identical suites twice",
    )
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
        fault_group=args.fault_group,
        clickhouse_driver=args.clickhouse_driver,
    )


if __name__ == "__main__":
    raise SystemExit(main())
