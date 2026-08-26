from __future__ import annotations

import importlib.util
import json
import types
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from analytics_toolkit.sql.connection import get_connection_config

from release_routines import sql_integration
from tests.sql.integration import conftest as integration_conftest
from tests.sql.integration.auth import auth as integration_auth


@pytest.mark.parametrize(
    ("driver", "port"),
    [("http", 18123), ("native", 19000)],
)
def test_canonical_clickhouse_alias_selects_integration_driver(
    monkeypatch: pytest.MonkeyPatch,
    driver: str,
    port: int,
) -> None:
    monkeypatch.setenv("SQL_INTEGRATION_CLICKHOUSE_DRIVER", driver)

    connections = integration_conftest._integration_connections()

    assert connections["ch"]["driver"] == driver
    assert connections["ch"]["port"] == port
    assert connections["ch_source"] == connections["ch"]
    assert connections["ch_target"] == connections["ch"]
    assert connections["ch_limited"]["driver"] == "http"


@pytest.mark.parametrize(
    ("driver", "tls_port", "hostname_port"),
    [("http", 18444, 18448), ("native", 19440, 19448)],
)
def test_clickhouse_auth_aliases_select_transport_tls_frontends(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    driver: str,
    tls_port: int,
    hostname_port: int,
) -> None:
    monkeypatch.setenv("SQL_INTEGRATION_PROFILE", "auth")
    monkeypatch.setenv("SQL_INTEGRATION_CERTS", str(tmp_path))
    monkeypatch.setenv("SQL_INTEGRATION_CLICKHOUSE_DRIVER", driver)

    connections = integration_conftest._integration_connections()

    assert connections["ch_tls"]["driver"] == driver
    assert connections["ch_tls"]["port"] == tls_port
    assert connections["ch_hostname_tls"]["port"] == hostname_port


def test_run_executes_every_profile_for_both_clickhouse_drivers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    def fake_run_profile(**kwargs) -> int:
        calls.append((kwargs["profile"], kwargs["clickhouse_driver"]))
        return 0

    monkeypatch.setattr(sql_integration, "run_profile", fake_run_profile)
    monkeypatch.setattr(
        sql_integration,
        "_assert_transport_scenario_parity",
        lambda **_kwargs: 0,
    )

    result = sql_integration.run(
        profile="all",
        include_greenplum=True,
        clickhouse_driver="both",
    )

    assert result == 0
    assert calls == [
        (profile, driver)
        for profile in ("core", "auth", "fault", "stress")
        for driver in ("http", "native")
    ]


def test_integration_pytest_command_bounds_each_test(tmp_path: Path) -> None:
    command = sql_integration._pytest_command("core", tmp_path)

    assert f"--timeout={sql_integration.TEST_TIMEOUT_SECONDS}" in command
    assert "--timeout-method=signal" in command


def test_airflow_plain_http_trino_route_never_uses_basic_auth_password() -> None:
    uris = integration_auth._airflow_connection_uris(
        clickhouse_driver="http",
        include_greenplum=False,
    )

    parsed = urlsplit(uris["airflow_trino"])
    assert parsed.scheme == "http"
    assert parsed.username == "integration"
    assert parsed.password is None


def test_dedicated_https_trino_basic_route_retains_authentication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("SQL_INTEGRATION_PROFILE", "auth")
    monkeypatch.setenv("SQL_INTEGRATION_CERTS", str(tmp_path))

    connection = integration_conftest._integration_connections()["trino_basic_tls"]

    assert connection["http_scheme"] == "https"
    assert connection["auth_mode"] == "basic"
    assert connection["password"] == "integration"


def test_native_profile_fails_before_compose_when_driver_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(
        sql_integration.importlib.util,
        "find_spec",
        lambda name: None if name == "clickhouse_driver" else real_find_spec(name),
    )
    monkeypatch.setattr(
        sql_integration,
        "_run",
        lambda *_args, **_kwargs: pytest.fail("compose must not start"),
    )

    assert (
        sql_integration.run_profile(
            profile="core",
            include_greenplum=False,
            clickhouse_driver="native",
        )
        == 2
    )


def test_transport_scenario_parity_rejects_different_collections(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(sql_integration, "ARTIFACTS_DIR", tmp_path)
    for driver, scenarios in (
        ("http", [{"scenario_id": "same", "node_id": "test_same"}]),
        ("native", [{"scenario_id": "different", "node_id": "test_different"}]),
    ):
        directory = tmp_path / "core" / driver
        directory.mkdir(parents=True)
        (directory / "collected-scenarios.json").write_text(
            json.dumps(scenarios),
            encoding="utf-8",
        )

    assert sql_integration._assert_transport_scenario_parity(profile="core") == 1
    report = json.loads((tmp_path / "core" / "transport-parity.json").read_text())
    assert report["matches"] is False


def test_greenplum_healthcheck_waits_for_stable_final_postmaster() -> None:
    compose = sql_integration.CORE_COMPOSE_FILE.read_text(encoding="utf-8")

    assert "/usr/local/greenplum-db/bin/psql" in compose
    assert "-h 127.0.0.1" in compose
    assert "pg_postmaster_start_time()" in compose
    assert "/data/.auth-tls-ready" in compose


def test_iceberg_integration_namespaces_are_preseeded() -> None:
    init_sql = (sql_integration.INTEGRATION_DIR / "iceberg-catalog" / "init.sql").read_text(
        encoding="utf-8"
    )

    assert "('analytics_toolkit', 'integration', 'exists', 'true')" in init_sql
    assert "('analytics_toolkit', 'integration_stage', 'exists', 'true')" in init_sql
    assert integration_conftest._RUNTIME_SCHEMA_STATEMENTS == ()
    assert (
        integration_conftest._integration_connections()["trino"]["s3_transfer_staging_schema"]
        == "hive.default"
    )


def test_integration_connections_explicitly_activate_loopback_file(tmp_path: Path) -> None:
    def write(connections: dict[str, dict[str, object]]) -> Path:
        path = tmp_path / ".connections"
        path.write_text(json.dumps(connections), encoding="utf-8")
        return path

    try:
        activated_path = integration_conftest._activate_integration_connections(write)
        config = get_connection_config("trino")

        assert activated_path == tmp_path / ".connections"
        assert config.host == "127.0.0.1"
        assert config.catalog == "iceberg"
    finally:
        integration_conftest.general_module.set_connections_path(None)


def test_greenplum_auth_uses_native_tls_behind_tcp_passthrough() -> None:
    compose = sql_integration.AUTH_COMPOSE_FILE.read_text(encoding="utf-8")
    haproxy = (sql_integration.INTEGRATION_DIR / "haproxy" / "auth.cfg").read_text(encoding="utf-8")

    assert "configure-auth-tls.sh" in compose
    assert "SQL_INTEGRATION_REQUIRE_GP_TLS_READY" in compose
    assert "chmod 0644 /certs/server.key" in compose
    assert "bind *:19432\n" in haproxy
    assert "bind *:19432 ssl" not in haproxy


def test_auth_trino_healthchecks_do_not_require_authentication() -> None:
    compose = sql_integration.AUTH_COMPOSE_FILE.read_text(encoding="utf-8")

    assert "&auth-trino-healthcheck" in compose
    assert compose.count("*auth-trino-healthcheck") == 2
    assert "</dev/tcp/127.0.0.1/8080" in compose


def test_auth_certificates_are_streamed_as_root_instead_of_compose_cp() -> None:
    runner = sql_integration.__file__
    assert runner is not None
    source = Path(runner).read_text(encoding="utf-8")

    assert '"--user",\n                        "root"' in source
    assert 'f"/certs/{filename}"' in source
    assert '"cp",\n                        f"auth-' not in source
    assert 'f"auth-proxy:/certs/{filename}"' not in source


def test_greenplum_tls_readiness_requires_three_consecutive_connections(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    outcomes = iter([RuntimeError("recovering"), object(), object(), object()])
    closed: list[object] = []

    def connect(**_kwargs):
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return types.SimpleNamespace(close=lambda: closed.append(outcome))

    monkeypatch.setattr(
        sql_integration,
        "psycopg2",
        types.SimpleNamespace(connect=connect),
    )
    monkeypatch.setattr(sql_integration.time, "sleep", lambda _seconds: None)

    assert sql_integration._wait_for_greenplum_tls(tmp_path) == 0
    assert len(closed) == 3
    report = json.loads((tmp_path.parent / "greenplum-tls-readiness.json").read_text())
    assert report["ready"] is True
    assert len(report["attempts"]) == 4
