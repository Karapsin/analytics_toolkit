from __future__ import annotations

# ruff: noqa: I001, PT011, UP037

import base64
import copy
import hashlib
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from analytics_toolkit import sql
from tests.sql.integration.manifest import scenario_param

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

GP_CERT_CASES = (
    "missing",
    "invalid",
    "wrong_key",
)

_BROWSER_LOGIN_ATTEMPTS = 3
_BROWSER_LOGIN_TIMEOUT_MS = 15_000


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


@pytest.mark.parametrize(
    "failure",
    [scenario_param(f"auth.cert.gp.{failure}", failure) for failure in GP_CERT_CASES],
)
def test_greenplum_client_certificate_failures(
    failure: str,
    integration_connections: dict[str, dict[str, object]],
    write_sql_connections: "Callable[[dict[str, dict[str, object]]], Path]",
) -> None:
    if os.environ.get("SQL_INTEGRATION_GP") != "1":
        pytest.skip("Greenplum authentication requires x86_64")
    certs = Path(os.environ["SQL_INTEGRATION_CERTS"])
    invalid = copy.deepcopy(integration_connections["gp_tls"])
    if failure == "missing":
        invalid.pop("ssl_cert", None)
        invalid.pop("ssl_key", None)
    elif failure == "invalid":
        invalid["ssl_cert"] = str(certs / "invalid-client.crt")
        invalid["ssl_key"] = str(certs / "invalid-client.key")
    else:
        invalid["ssl_key"] = str(certs / "invalid-client.key")
    write_sql_connections({f"gp_cert_{failure}": invalid})

    with pytest.raises(Exception) as exc_info:
        sql.read(f"gp_cert_{failure}", "SELECT 1", retry_cnt=1)
    rendered = repr(exc_info.value)
    assert "PRIVATE KEY" not in rendered
    assert "integration-secret" not in rendered


@pytest.mark.parametrize(
    ("backend", "alias"),
    [
        scenario_param("auth.cert.trino.hostname", "trino", "trino_hostname_tls"),
        scenario_param("auth.cert.ch.hostname", "ch", "ch_hostname_tls"),
    ],
)
def test_tls_hostname_mismatch_fails(
    backend: str,
    alias: str,
) -> None:
    with pytest.raises(Exception) as exc_info:
        sql.read(alias, "SELECT 1", retry_cnt=1)
    rendered = repr(exc_info.value).lower()
    assert backend in {"trino", "ch"}
    assert any(token in rendered for token in ("hostname", "certificate", "verify", "ssl"))


def _install_browser_oauth(
    monkeypatch: pytest.MonkeyPatch,
    *,
    password: str = "integration",
    alias: str = "trino_oauth_tls",
    before_callback=None,
):
    try:
        import trino.auth  # noqa: PLC0415 - auth profile dependency.
        from cryptography import x509  # noqa: PLC0415
        from cryptography.hazmat.primitives import serialization  # noqa: PLC0415
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError  # noqa: PLC0415
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
            username = _open_keycloak_login(page, url, PlaywrightTimeoutError)
            username.fill("integration")
            page.locator("#password").fill(password)
            page.locator("#kc-login").click(no_wait_after=before_callback is not None)
            if before_callback is not None:
                before_callback()
            page.wait_for_url("**/oauth2/**", timeout=10_000)
            browser.close()

    authentication = real_oauth(redirect)
    monkeypatch.setattr(trino.auth, "OAuth2Authentication", lambda: authentication)
    return callbacks, alias


def _open_keycloak_login(page, url: str, timeout_error: type[BaseException]):
    last_error: BaseException | None = None
    for attempt in range(_BROWSER_LOGIN_ATTEMPTS):
        try:
            if attempt == 0:
                page.goto(url, wait_until="domcontentloaded")
            else:
                page.reload(wait_until="domcontentloaded")
            username = page.locator("#username")
            username.wait_for(state="visible", timeout=_BROWSER_LOGIN_TIMEOUT_MS)
        except timeout_error as exc:  # noqa: PERF203 - browser waits are intentionally retried.
            last_error = exc
        else:
            return username
    if last_error is None:  # pragma: no cover - the retry range is non-empty.
        message = "Keycloak login retry did not run."
        raise RuntimeError(message)
    raise last_error


@pytest.mark.sql_scenario("auth.oauth.reuse")
def test_oauth_authentication_object_reuses_valid_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callbacks, _ = _install_browser_oauth(monkeypatch)
    first = sql.read("trino_oauth_tls", "SELECT 11 AS value", retry_cnt=1)
    second = sql.read("trino_oauth_tls", "SELECT 12 AS value", retry_cnt=1)
    assert int(first.iloc[0, 0]) == 11
    assert int(second.iloc[0, 0]) == 12
    assert len(callbacks) == 1


@pytest.mark.sql_scenario("auth.oauth.lifecycle.expiry")
def test_oauth_expiry_refreshes_without_second_browser_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callbacks, _ = _install_browser_oauth(monkeypatch)
    first = sql.read("trino_oauth_tls", "SELECT 13 AS value", retry_cnt=1)
    assert int(first.iloc[0, 0]) == 13
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline - 3:
        time.sleep(0.25)
    refreshed = sql.read("trino_oauth_tls", "SELECT 14 AS value", retry_cnt=1)
    assert int(refreshed.iloc[0, 0]) == 14
    assert len(callbacks) == 1


@pytest.mark.sql_scenario("auth.oauth.lifecycle.wrong_password")
def test_oauth_wrong_user_password_fails_without_secret_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_browser_oauth(monkeypatch, password="wrong-browser-password")
    with pytest.raises(Exception) as exc_info:
        sql.read("trino_oauth_tls", "SELECT 15 AS value", retry_cnt=1)
    rendered = repr(exc_info.value)
    assert "wrong-browser-password" not in rendered
    assert "access_token" not in rendered.lower()


@pytest.mark.sql_scenario("auth.oauth.lifecycle.invalid_client_secret")
def test_oauth_invalid_client_secret_fails_without_secret_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_browser_oauth(monkeypatch, alias="trino_invalid_oauth_tls")
    with pytest.raises(Exception) as exc_info:
        sql.read("trino_invalid_oauth_tls", "SELECT 16 AS value", retry_cnt=1)
    rendered = repr(exc_info.value)
    assert "invalid-integration-oauth-secret" not in rendered
    assert "access_token" not in rendered.lower()
