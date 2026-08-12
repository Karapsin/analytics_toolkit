from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from analytics_toolkit import sql
from tests.integration.manifest import scenario_param

if TYPE_CHECKING:
    from collections.abc import Callable


pytestmark = [pytest.mark.integration, pytest.mark.integration_auth]


def _auth_aliases() -> list[str]:
    aliases = ["trino_basic_tls", "ch_tls"]
    if os.environ.get("SQL_INTEGRATION_GP") == "1":
        aliases.append("gp_tls")
    return aliases


@pytest.mark.parametrize(
    "alias",
    [
        scenario_param("auth.trino.basic", "trino_basic_tls"),
        scenario_param("auth.clickhouse.tls", "ch_tls"),
        scenario_param("auth.greenplum.mtls", "gp_tls"),
    ],
)
def test_tls_connections_validate_and_execute(alias: str) -> None:
    if alias not in _auth_aliases():
        pytest.skip("Greenplum authentication requires x86_64")
    parsed = sql.validate_connections([alias], connect=False)
    connected = sql.validate_connections([alias], connect=True)

    assert parsed[0].valid
    assert connected[0].valid
    assert connected[0].connected
    frame = sql.read(alias, "SELECT 1 AS value")
    assert int(frame.iloc[0, 0]) == 1


@pytest.mark.sql_scenario("auth.airflow.routes")
def test_real_airflow_connection_source_routes_all_backends(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, object]], Path],
) -> None:
    import airflow.models  # noqa: F401, PLC0415 - initialize all ORM models.
    from airflow.hooks.base import BaseHook  # noqa: PLC0415 - auth-profile dependency.
    from airflow.secrets.environment_variables import (  # noqa: PLC0415
        EnvironmentVariablesBackend,
    )

    aliases = {
        "airflow_trino": {"type": "trino"},
        "airflow_ch": {"type": "ch"},
    }
    monkeypatch.setenv(
        "AIRFLOW_CONN_AIRFLOW_TRINO",
        "http://integration:integration@127.0.0.1:18080/iceberg",
    )
    monkeypatch.setenv(
        "AIRFLOW_CONN_AIRFLOW_CH",
        (
            "http://integration:integration@127.0.0.1:19000/integration?driver=native"
            if os.environ.get("SQL_INTEGRATION_CLICKHOUSE_DRIVER") == "native"
            else "http://integration:integration@127.0.0.1:18123/integration"
        ),
    )
    if os.environ.get("SQL_INTEGRATION_GP") == "1":
        aliases["airflow_gp"] = {"type": "gp"}
        monkeypatch.setenv(
            "AIRFLOW_CONN_AIRFLOW_GP",
            "postgresql://gpadmin:integration@127.0.0.1:15432/analytics_toolkit",
        )
    environment_backend = EnvironmentVariablesBackend()
    monkeypatch.setattr(
        BaseHook,
        "get_connection",
        staticmethod(environment_backend.get_connection),
    )
    write_sql_connections({"source": "airflow", "connections": aliases})

    results = sql.validate_connections(list(aliases), connect=True)
    assert all(result.valid and result.connected for result in results), [
        (result.connection_key, result.valid, result.connected, result.error) for result in results
    ]


@pytest.mark.sql_scenario("auth.trino.oauth")
def test_real_browser_driven_trino_oauth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    try:
        import trino.auth  # noqa: PLC0415 - optional auth-profile dependency.
        from cryptography import x509  # noqa: PLC0415 - optional auth-profile dependency.
        from cryptography.hazmat.primitives import serialization  # noqa: PLC0415
        from playwright.sync_api import sync_playwright  # noqa: PLC0415 - optional dependency.
    except ImportError as exc:  # pragma: no cover - auth job installs pinned dependencies.
        pytest.fail(f"auth integration dependencies are missing: {exc}")

    real_oauth = trino.auth.OAuth2Authentication
    browser_log: list[str] = []
    cert_path = Path(os.environ["SQL_INTEGRATION_CERTS"]) / "server.crt"
    certificate = x509.load_pem_x509_certificate(cert_path.read_bytes())
    public_key = certificate.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    spki_pin = base64.b64encode(hashlib.sha256(public_key).digest()).decode("ascii")

    def browser_redirect(url: str) -> None:
        browser_log.append(url)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                args=[f"--ignore-certificate-errors-spki-list={spki_pin}"],
            )
            context = browser.new_context()
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded")
            page.locator("#username").fill("integration")
            page.locator("#password").fill("integration")
            page.locator("#kc-login").click()
            try:
                page.wait_for_url("**/oauth2/**", timeout=10_000)
            finally:
                browser_log.extend((page.url, page.title()))
                artifact_dir = Path(os.environ["SQL_INTEGRATION_ARTIFACT_DIR"])
                (artifact_dir / "oauth-browser.log").write_text(
                    "\n".join(browser_log), encoding="utf-8"
                )
            browser.close()

    monkeypatch.setattr(
        trino.auth,
        "OAuth2Authentication",
        lambda: real_oauth(browser_redirect),
    )
    frame = sql.read("trino_oauth_tls", "SELECT 1 AS value", retry_cnt=1)
    assert int(frame.iloc[0, 0]) == 1
    artifact_dir = Path(os.environ["SQL_INTEGRATION_ARTIFACT_DIR"])
    (artifact_dir / "oauth-browser.log").write_text("\n".join(browser_log), encoding="utf-8")


@pytest.mark.sql_scenario("auth.realm.secret_free")
def test_auth_environment_is_secret_free() -> None:
    realm_path = Path(__file__).parents[2] / "integration/keycloak/integration-realm.json"
    realm = json.loads(realm_path.read_text(encoding="utf-8"))
    assert realm["realm"] == "integration"
