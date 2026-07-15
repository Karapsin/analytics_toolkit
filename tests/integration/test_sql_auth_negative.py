from __future__ import annotations

# ruff: noqa: I001, PT011, UP037

import base64
import copy
import hashlib
import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from analytics_toolkit import sql
from tests.integration.manifest import scenario_param

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = [pytest.mark.integration, pytest.mark.integration_auth]

CASES = (
    ("trino", "trino_basic_tls", "password"),
    ("trino", "trino_basic_tls", "ca"),
    ("ch", "ch_tls", "password"),
    ("ch", "ch_tls", "ca"),
    ("gp", "gp_tls", "password"),
    ("gp", "gp_tls", "ca"),
)


@pytest.mark.parametrize(
    ("backend", "valid_alias", "failure"),
    [
        scenario_param(f"auth.negative.{backend}.{failure}", backend, alias, failure)
        for backend, alias, failure in CASES
    ],
)
def test_negative_tls_credentials_fail_during_real_connection(
    backend: str,
    valid_alias: str,
    failure: str,
    integration_connections: dict[str, dict[str, object]],
    write_sql_connections: "Callable[[dict[str, dict[str, object]]], Path]",
) -> None:
    if backend == "gp" and os.environ.get("SQL_INTEGRATION_GP") != "1":
        pytest.skip("Greenplum authentication requires x86_64")
    invalid_alias = f"invalid_{backend}_{failure}"
    invalid = copy.deepcopy(integration_connections[valid_alias])
    secret = "intentionally-wrong-password"
    if failure == "password":
        invalid["password"] = secret
    else:
        invalid["ca_certs"] = [str(Path(os.environ["SQL_INTEGRATION_CERTS"]) / "wrong-ca.crt")]
    write_sql_connections({invalid_alias: invalid})

    with pytest.raises(Exception) as exc_info:
        sql.read(invalid_alias, "SELECT 1", retry_cnt=1)
    rendered = repr(exc_info.value)
    assert secret not in rendered
    assert "PRIVATE KEY" not in rendered
    if failure == "password":
        assert any(
            token in rendered.lower()
            for token in ("auth", "password", "credential", "access denied")
        )
    else:
        assert any(token in rendered.lower() for token in ("certificate", "verify", "ssl", "tls"))


@pytest.mark.sql_scenario("auth.oauth.reuse")
def test_oauth_authentication_object_reuses_valid_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    try:
        import trino.auth  # noqa: PLC0415 - auth profile dependency.
        from cryptography import x509  # noqa: PLC0415
        from cryptography.hazmat.primitives import serialization  # noqa: PLC0415
        from playwright.sync_api import sync_playwright  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        pytest.fail(f"auth integration dependency is missing: {exc}")

    real_oauth = trino.auth.OAuth2Authentication
    callbacks: list[str] = []
    cert_path = Path(os.environ["SQL_INTEGRATION_CERTS"]) / "server.crt"
    certificate = x509.load_pem_x509_certificate(cert_path.read_bytes())
    public_key = certificate.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    spki_pin = base64.b64encode(hashlib.sha256(public_key).digest()).decode("ascii")

    def redirect(url: str) -> None:
        callbacks.append(url)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                args=[f"--ignore-certificate-errors-spki-list={spki_pin}"],
            )
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded")
            page.locator("#username").fill("integration")
            page.locator("#password").fill("integration")
            page.locator("#kc-login").click()
            page.wait_for_url("**/oauth2/**", timeout=10_000)
            browser.close()

    authentication = real_oauth(redirect)
    monkeypatch.setattr(trino.auth, "OAuth2Authentication", lambda: authentication)
    first = sql.read("trino_oauth_tls", "SELECT 11 AS value", retry_cnt=1)
    second = sql.read("trino_oauth_tls", "SELECT 12 AS value", retry_cnt=1)
    assert int(first.iloc[0, 0]) == 11
    assert int(second.iloc[0, 0]) == 12
    assert len(callbacks) == 1
