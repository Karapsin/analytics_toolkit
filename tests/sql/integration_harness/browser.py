from __future__ import annotations

import json
import types
from typing import TYPE_CHECKING

import pytest

from tests.sql.integration.auth import negative

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    "failure",
    [
        ("launch", False, "launch"),
        ("new-page", False, "new-page"),
        ("goto", False, "open-login"),
        ("visibility", False, "open-login"),
        ("username", False, "username"),
        ("password", False, "password"),
        ("submit", False, "submit-login"),
        ("before-callback", False, "before-callback"),
        ("wait-callback", False, "wait-callback"),
        ("close", False, "close"),
        ("goto", True, "open-login"),
        (None, False, "complete"),
    ],
)
def test_browser_failure_retains_complete_redacted_diagnostics_and_closes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: tuple[str | None, bool, str],
) -> None:
    failed_step, close_fails, phase = failure
    monkeypatch.setenv("SQL_INTEGRATION_ARTIFACT_DIR", str(tmp_path))
    monkeypatch.setenv("SQL_INTEGRATION_TEST_ID", "exchange")
    url = (
        "https://user:url-password@localhost/oauth2/token/initiate/state-secret"
        "?code=query-secret#fragment-secret"
    )
    password = "browser-password"
    message = (
        "Page.goto: net::ERR_CONNECTION_REFUSED "
        + "details " * 1000
        + f" {url} password={password} client_secret=integration-oauth-secret "
        "access_token=token-secret Authorization: Bearer bearer-secret\n"
        "-----BEGIN PRIVATE KEY-----\nkey-secret\n"
        "-----END PRIVATE KEY-----\nfinal diagnostic line"
    )
    error = RuntimeError(message)
    close_error = RuntimeError("secondary close failure")
    calls: list[str] = []

    def step(name: str):
        calls.append(name)
        if name == failed_step:
            raise error
        if name == "close" and close_fails:
            raise close_error

    def click(**kwargs) -> None:
        assert kwargs == {"no_wait_after": True}
        step("submit")

    def locator(selector: str):
        name = {"#username": "username", "#password": "password", "#kc-login": "submit"}[selector]
        return types.SimpleNamespace(
            wait_for=lambda **_kwargs: step("visibility"),
            fill=lambda _value: step(name),
            click=click,
        )

    def goto(attempted_url: str, **kwargs) -> None:
        assert attempted_url == url
        assert kwargs == {"wait_until": "domcontentloaded"}
        step("goto")

    def wait_for_url(pattern: str, **kwargs) -> None:
        assert pattern == "**/oauth2/**"
        assert kwargs == {"timeout": 10_000}
        step("wait-callback")

    page = types.SimpleNamespace(
        goto=goto,
        locator=locator,
        wait_for_url=wait_for_url,
    )

    def new_page():
        step("new-page")
        return page

    browser = types.SimpleNamespace(new_page=new_page, close=lambda: step("close"))

    def launch():
        step("launch")
        return browser

    def before_callback() -> None:
        step("before-callback")

    if failed_step is None:
        negative._run_browser_oauth(launch, url, password, before_callback, TimeoutError)
    else:
        with pytest.raises(RuntimeError) as exc_info:
            negative._run_browser_oauth(launch, url, password, before_callback, TimeoutError)
        assert exc_info.value is error
    assert calls.count("close") == (0 if failed_step == "launch" else 1)
    if failed_step in {"launch", "new-page", "goto", "visibility", "username", "password", "submit"}:
        assert "before-callback" not in calls
    else:
        assert calls.index("submit") < calls.index("before-callback")
    records = [
        json.loads(line)
        for line in (tmp_path / "oauth-browser.log").read_text(encoding="utf-8").splitlines()
    ]
    assert records[0]["phase"] == phase
    assert records[0]["scenario_id"] == "exchange"
    rendered = json.dumps(records)
    for secret in (
        password,
        "url-password",
        "state-secret",
        "query-secret",
        "fragment-secret",
        "integration-oauth-secret",
        "token-secret",
        "bearer-secret",
        "key-secret",
    ):
        assert secret not in rendered
    if failed_step is not None:
        assert "net::ERR_CONNECTION_REFUSED" in records[0]["exception"]
        assert "details " * 1000 in records[0]["exception"]
        assert "final diagnostic line" in records[0]["exception"]
    if close_fails:
        assert records[1]["phase"] == "close"
        assert "secondary close failure" in records[1]["exception"]

    monkeypatch.setenv("SQL_INTEGRATION_TEST_ID", "later")
    negative._browser_diagnostic("complete", url, password)
    retained = (tmp_path / "oauth-browser.log").read_text(encoding="utf-8").splitlines()
    assert len(retained) == len(records) + 1
    assert json.loads(retained[0]) == records[0]
    assert json.loads(retained[-1])["scenario_id"] == "later"
