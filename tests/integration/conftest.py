from __future__ import annotations

# ruff: noqa: BLE001, I001, PERF203

import json
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from analytics_toolkit import sql
from tests.integration.support.faults import FaultController
from tests.integration.support.identity import safe_identifier
from tests.integration.support.resources import ResourceRegistry

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


LOOPBACK_HOSTS = {"127.0.0.1", "localhost"}


def pytest_configure(config: pytest.Config) -> None:
    # Python 3.14 reports this from a transitive database-driver import.  The
    # integration job pins that driver and treats every actionable warning as a
    # failure, so suppress only this upstream array('u') deprecation.
    config.addinivalue_line(
        "filterwarnings",
        "ignore:The 'u' type code is deprecated.*:DeprecationWarning",
    )


@pytest.fixture
def fault_controller() -> Iterator[FaultController]:
    if os.environ.get("SQL_INTEGRATION_PROFILE") != "fault":
        pytest.skip("fault controller is available only in the fault profile")
    controller = FaultController(
        root=Path(__file__).parents[2],
        project=os.environ["SQL_INTEGRATION_COMPOSE_PROJECT"],
        artifact_dir=Path(os.environ["SQL_INTEGRATION_ARTIFACT_DIR"]),
    )
    try:
        yield controller
    finally:
        controller.restore()


def pytest_runtest_setup(item: pytest.Item) -> None:
    marker = item.get_closest_marker("sql_scenario")
    value = marker.args[0] if marker and marker.args else item.nodeid
    os.environ["SQL_INTEGRATION_TEST_ID"] = safe_identifier(str(value), limit=24)


def pytest_collection_finish(session: pytest.Session) -> None:
    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in session.items:
        marker = item.get_closest_marker("sql_scenario")
        if marker is None:
            continue
        if len(marker.args) != 1 or not isinstance(marker.args[0], str):
            msg = f"invalid sql_scenario marker on {item.nodeid}"
            raise pytest.UsageError(msg)
        scenario_id = marker.args[0]
        if scenario_id in seen:
            msg = f"duplicate SQL scenario ID collected: {scenario_id}"
            raise pytest.UsageError(msg)
        seen.add(scenario_id)
        records.append({"scenario_id": scenario_id, "node_id": item.nodeid})
    artifact_dir = Path(
        os.environ.get(
            "SQL_INTEGRATION_ARTIFACT_DIR",
            ".integration-artifacts/collection",
        )
    )
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "collected-scenarios.json").write_text(
        json.dumps(sorted(records, key=lambda item: item["scenario_id"]), indent=2),
        encoding="utf-8",
    )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[object]):
    outcome = yield
    report = outcome.get_result()
    setattr(item, f"rep_{report.when}", report)


def _integration_connections() -> dict[str, dict[str, object]]:
    connections: dict[str, dict[str, object]] = {
        "trino": {
            "type": "trino",
            "host": "127.0.0.1",
            "port": int(os.environ.get("SQL_INTEGRATION_TRINO_PORT", "18080")),
            "user": "integration",
            "catalog": "iceberg",
            "schema": "integration",
            "http_scheme": "http",
            "verify": False,
            "insert_chunk_size": 100,
            "transfer_staging_schema": "iceberg.integration_stage",
            "s3_transfer_staging_schema": "hive.integration_stage",
            "s3_transfer_staging_location": "s3a://warehouse/staging",
            "upsert_partition_drop_sql_template": (
                "DELETE FROM {table} WHERE {partition_column} = "
                "CAST(from_iso8601_timestamp({partition_value}) AS TIMESTAMP)"
            ),
        },
        "ch": {
            "type": "ch",
            "host": "127.0.0.1",
            "port": int(os.environ.get("SQL_INTEGRATION_CLICKHOUSE_PORT", "18123")),
            "user": "integration",
            "password": "integration",
            "database": "integration",
            "secure": False,
            "transfer_staging_schema": "integration",
            "ddl_defaults": {
                "regular": {
                    "create_distributed_pair": True,
                    "shard": {
                        "engine": "MergeTree",
                        "on_cluster": "integration_cluster",
                    },
                    "distributed": {
                        "engine_template": (
                            "Distributed({cluster}, {database}, {shard_table}, {sharding_key})"
                        ),
                        "cluster": "integration_cluster",
                        "on_cluster": "integration_cluster",
                        "sharding_key": "cityHash64(randCanonical())",
                    },
                },
                "staging": {
                    "create_distributed_pair": False,
                    "shard": {"engine": "MergeTree", "on_cluster": None},
                },
            },
        },
    }
    connections["trino_values"] = {
        **connections["trino"],
        "s3_transfer_staging_schema": None,
        "s3_transfer_staging_location": None,
        "insert_chunk_size": 2,
    }
    connections["trino_parquet"] = {
        **connections["trino"],
        "aws_access_key_id": "integration",
        "aws_secret_access_key": "integration-secret",
        "aws_endpoint_url": (
            "http://127.0.0.1:" + os.environ.get("SQL_INTEGRATION_MINIO_PORT", "19001")
        ),
    }
    connections["trino_source_values"] = {**connections["trino_values"]}
    connections["trino_target_values"] = {**connections["trino_values"]}
    connections["trino_source_parquet"] = {**connections["trino_parquet"]}
    connections["trino_target_parquet"] = {**connections["trino_parquet"]}
    connections["ch_limited"] = {
        **connections["ch"],
        "query_limit": 2,
        "query_retries": 1,
        "client_name": "analytics-toolkit-integration",
        "settings": {"max_execution_time": 60},
    }
    connections["ch_source"] = {**connections["ch"]}
    connections["ch_target"] = {**connections["ch"]}
    connections["ch_native"] = {
        **connections["ch"],
        "driver": "native",
        "port": int(os.environ.get("SQL_INTEGRATION_CLICKHOUSE_NATIVE_PORT", "19000")),
        "compression": False,
    }
    if os.environ.get("SQL_INTEGRATION_PROFILE") == "stress":
        connections["trino_pressure"] = {
            **connections["trino_values"],
            "port": int(os.environ.get("SQL_INTEGRATION_PRESSURE_PORT", "18082")),
            "connect_timeout": 2,
        }
    if os.environ.get("SQL_INTEGRATION_GP") == "1":
        connections["gp"] = {
            "type": "gp",
            "host": "127.0.0.1",
            "port": int(os.environ.get("SQL_INTEGRATION_GREENPLUM_PORT", "15432")),
            "user": "gpadmin",
            "password": "integration",
            "database": "analytics_toolkit",
            "sslmode": "disable",
            "transfer_staging_schema": "public",
        }
        connections["gp_alias"] = {**connections["gp"]}
        connections["gp_source"] = {**connections["gp"]}
        connections["gp_target"] = {**connections["gp"]}
    if os.environ.get("SQL_INTEGRATION_PROFILE") in {"auth", "fault"}:
        certs = os.environ["SQL_INTEGRATION_CERTS"]
        connections["trino_basic_tls"] = {
            **connections["trino_values"],
            "port": int(os.environ.get("SQL_INTEGRATION_TRINO_TLS_PORT", "18443")),
            "password": "integration",
            "auth_mode": "basic",
            "http_scheme": "https",
            "ca_certs": [f"{certs}/ca.crt"],
            "verify": True,
        }
        connections["trino_oauth_tls"] = {
            **connections["trino_parquet"],
            "port": int(os.environ.get("SQL_INTEGRATION_TRINO_OAUTH_TLS_PORT", "18446")),
            "auth_mode": "oauth2",
            "http_scheme": "https",
            "ca_certs": [f"{certs}/ca.crt"],
            "verify": True,
            "source": "analytics-toolkit-integration-oauth",
        }
        connections["trino_oauth_discovery_tls"] = {**connections["trino_oauth_tls"]}
        connections["trino_oauth_exchange_tls"] = {**connections["trino_oauth_tls"]}
        connections["trino_invalid_oauth_tls"] = {
            **connections["trino_oauth_tls"],
            "port": int(os.environ.get("SQL_INTEGRATION_TRINO_INVALID_OAUTH_TLS_PORT", "18449")),
        }
        connections["ch_tls"] = {
            **connections["ch"],
            "port": int(os.environ.get("SQL_INTEGRATION_CLICKHOUSE_TLS_PORT", "18444")),
            "secure": True,
            "verify": True,
            "ca_certs": [f"{certs}/ca.crt"],
        }
        connections["ch_tls_variable_ca"] = {
            **connections["ch_tls"],
            "ca_certs": [f"{certs}/ca.crt", f"{certs}/ca-copy.crt"],
        }
        connections["trino_hostname_tls"] = {
            **connections["trino_basic_tls"],
            "port": int(os.environ.get("SQL_INTEGRATION_TRINO_HOSTNAME_TLS_PORT", "18447")),
        }
        connections["ch_hostname_tls"] = {
            **connections["ch_tls"],
            "port": int(os.environ.get("SQL_INTEGRATION_CLICKHOUSE_HOSTNAME_TLS_PORT", "18448")),
        }
        if "gp" in connections:
            connections["gp_tls"] = {
                **connections["gp"],
                "port": int(os.environ.get("SQL_INTEGRATION_GP_TLS_PORT", "19432")),
                "sslmode": "verify-full",
                "ca_certs": [f"{certs}/ca.crt"],
                "ssl_cert": f"{certs}/client.crt",
                "ssl_key": f"{certs}/client.key",
            }
            connections["gp_tls_bundle"] = {
                **connections["gp_tls"],
                "ca_certs": [f"{certs}/ca.crt", f"{certs}/ca-copy.crt"],
            }
            connections["gp_tls_client_cert"] = {**connections["gp_tls"]}
    return connections


def _assert_loopback_connections(connections: dict[str, dict[str, object]]) -> None:
    for key, config in connections.items():
        host = config.get("host")
        if host not in LOOPBACK_HOSTS:
            message = f"Integration connection {key!r} is not loopback-only: {host!r}"
            raise RuntimeError(message)


@pytest.fixture
def integration_connections() -> dict[str, dict[str, object]]:
    connections = _integration_connections()
    _assert_loopback_connections(connections)
    return connections


@pytest.fixture(autouse=True)
def default_sql_connections(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    if os.environ.get("ANALYTICS_TOOLKIT_RUN_INTEGRATION") != "1":
        pytest.skip("integration tests require the repository integration workflow")
    connections = _integration_connections()
    _assert_loopback_connections(connections)
    monkeypatch.chdir(tmp_path)
    write_sql_connections(connections)


@pytest.fixture(autouse=True)
def initialize_integration_schemas(default_sql_connections: None) -> Iterator[None]:
    del default_sql_connections
    if os.environ.get("ANALYTICS_TOOLKIT_RUN_INTEGRATION") != "1":
        yield
        return
    sql.execute("trino", "CREATE SCHEMA IF NOT EXISTS iceberg.integration")
    sql.execute("trino", "CREATE SCHEMA IF NOT EXISTS iceberg.integration_stage")
    sql.execute("trino", "CREATE SCHEMA IF NOT EXISTS hive.integration_stage")
    yield


@pytest.fixture
def resource_registry(tmp_path: Path) -> ResourceRegistry:
    return ResourceRegistry(
        root=Path(__file__).parents[2],
        project=os.environ.get("SQL_INTEGRATION_COMPOSE_PROJECT"),
        artifact_dir=Path(os.environ.get("SQL_INTEGRATION_ARTIFACT_DIR", tmp_path / "artifacts")),
    )


@pytest.fixture(autouse=True)
def assert_no_toolkit_leaks(  # noqa: C901, PLR0912, PLR0915
    request: pytest.FixtureRequest,
    default_sql_connections: None,
    resource_registry: ResourceRegistry,
    tmp_path: Path,
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> Iterator[None]:
    del default_sql_connections
    yield
    write_sql_connections(_integration_connections())
    cleanup_errors = resource_registry.cleanup()
    leak_report = {
        "tables": [],
        "queries": [],
        "objects": [],
    }
    run_id = os.environ.get("SQL_INTEGRATION_RUN_ID", "")
    test_id = os.environ.get("SQL_INTEGRATION_TEST_ID", "")
    resource_prefix = f"it_{run_id}_{test_id}" if run_id and test_id else "it_stage_"
    backends = ["trino", "ch"]
    if os.environ.get("SQL_INTEGRATION_GP") == "1":
        backends.append("gp")
    for backend in backends:
        tables = sql.show_tables(backend)
        matching_tables = tables[
            tables["table_name"]
            .astype(str)
            .map(lambda name: name.startswith((resource_prefix, "it_stage_")))
        ]
        for row in matching_tables.itertuples(index=False):
            qualified = (
                f"{row.db}.{row.schema}.{row.table_name}"
                if backend == "trino"
                else f"{row.schema}.{row.table_name}"
            )
            try:
                sql.drop_tables(backend, qualified, if_exists=True)
            except Exception as exc:
                cleanup_errors.append(f"scan drop {backend}:{qualified}: {exc!r}")
        remaining_tables = sql.show_tables(backend)
        names = remaining_tables.get("table_name", []).tolist()
        leak_report["tables"].extend(
            f"{backend}:{name}"
            for name in names
            if str(name).startswith(resource_prefix) or str(name).startswith("it_stage_")
        )
        active = sql.show_queries(backend, state="active")
        if "query" in active:
            labelled = active[
                active["query"]
                .astype(str)
                .str.contains(
                    "analytics_toolkit_integration",
                    regex=False,
                )
            ]
            for query_id in labelled.get("query_id", []):
                try:
                    sql.cancel_queries(backend, [query_id], retry_cnt=1)
                except Exception as exc:
                    cleanup_errors.append(f"scan cancel {backend}:{query_id}: {exc!r}")
            active = sql.show_queries(backend, state="active")
            leak_report["queries"].extend(
                f"{backend}:{value}"
                for value in active["query"].astype(str)
                if "analytics_toolkit_integration" in value
            )
    compose_project = os.environ.get("SQL_INTEGRATION_COMPOSE_PROJECT")
    if compose_project:
        minio_command = [
            "docker",
            "compose",
            "--project-name",
            compose_project,
            "--file",
            str(Path(__file__).parents[2] / "integration/docker-compose.yml"),
            "exec",
            "-T",
            "minio-client",
            "mc",
        ]
        find_command = [
            *minio_command,
            "find",
            "integration/warehouse",
            "--name",
            f"*{resource_prefix}*",
            "--print",
        ]
        listed = subprocess.run(
            find_command,
            check=False,
            capture_output=True,
            text=True,
        )
        for object_path in listed.stdout.splitlines():
            removed = subprocess.run(
                [*minio_command, "rm", "--recursive", "--force", object_path],
                check=False,
                capture_output=True,
                text=True,
            )
            if removed.returncode != 0:
                cleanup_errors.append(f"scan minio {object_path}: {removed.stderr.strip()}")
        remaining = subprocess.run(
            find_command,
            check=False,
            capture_output=True,
            text=True,
        )
        leak_report["objects"] = remaining.stdout.splitlines()
    report_path = tmp_path / "integration-leaks.json"
    report_path.write_text(json.dumps(leak_report, indent=2), encoding="utf-8")
    artifact_dir = os.environ.get("SQL_INTEGRATION_ARTIFACT_DIR")
    if artifact_dir:
        artifact_path = Path(artifact_dir) / "leaks.json"
        previous = (
            json.loads(artifact_path.read_text(encoding="utf-8")) if artifact_path.exists() else []
        )
        previous.append({"test_id": test_id, **leak_report})
        artifact_path.write_text(json.dumps(previous, indent=2), encoding="utf-8")
    cleanup_report = {"cleanup_errors": cleanup_errors, "leaks": leak_report}
    if cleanup_errors or any(leak_report.values()):
        call_report = getattr(request.node, "rep_call", None)
        if call_report is not None and call_report.failed:
            request.node.add_report_section(
                "teardown",
                "integration cleanup",
                json.dumps(cleanup_report, indent=2),
            )
            return
        pytest.fail(f"integration cleanup failed: {cleanup_report}")
