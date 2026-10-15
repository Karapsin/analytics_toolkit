from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from tests.sql.integration.auth import negative
from tests.sql.integration_harness._support.browser import make_browser

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
    failed_step, close_fails, _phase = failure
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
    launch, before_callback, calls = make_browser(failed_step, close_fails, url, error)

    if failed_step is None:
        negative._run_browser_oauth(launch, url, password, before_callback, TimeoutError)
    else:
        with pytest.raises(RuntimeError) as exc_info:
            negative._run_browser_oauth(launch, url, password, before_callback, TimeoutError)
        assert exc_info.value is error
    _assert_operation_order(calls, failed_step)
    records = [
        json.loads(line)
        for line in (tmp_path / "oauth-browser.log").read_text(encoding="utf-8").splitlines()
    ]
    _assert_diagnostics(records, password, failure)
    _assert_appended_history(monkeypatch, tmp_path, url, password, records)


def _assert_operation_order(calls: list[str], failed_step: str | None) -> None:
    operations = [
        "launch",
        "new-page",
        "goto",
        "visibility",
        "username",
        "password",
        "submit",
        "before-callback",
        "wait-callback",
        "close",
    ]
    expected = (
        operations if failed_step is None else operations[: operations.index(failed_step) + 1]
    )
    if failed_step is not None and failed_step not in {"launch", "close"}:
        expected = [*expected, "close"]
    assert calls == expected
    assert calls.count("close") == (0 if failed_step == "launch" else 1)
    if failed_step in {
        "launch",
        "new-page",
        "goto",
        "visibility",
        "username",
        "password",
        "submit",
    }:
        assert "before-callback" not in calls
    else:
        assert calls.index("submit") < calls.index("before-callback")


def _assert_diagnostics(records, password: str, failure: tuple[str | None, bool, str]) -> None:
    failed_step, close_fails, phase = failure
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


def _assert_appended_history(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    url: str,
    password: str,
    records,
) -> None:
    monkeypatch.setenv("SQL_INTEGRATION_TEST_ID", "later")
    negative._browser_diagnostic("complete", url, password)
    retained = (tmp_path / "oauth-browser.log").read_text(encoding="utf-8").splitlines()
    assert len(retained) == len(records) + 1
    assert json.loads(retained[0]) == records[0]
    assert json.loads(retained[-1])["scenario_id"] == "later"


@pytest.mark.parametrize("failed_step", [None, "close"])
def test_browser_fake_records_close_before_raising_stable_error(failed_step: str | None) -> None:
    error = RuntimeError("primary close failure")
    launch, _before_callback, calls = make_browser(failed_step, True, "https://localhost", error)
    browser = launch()

    with pytest.raises(RuntimeError) as first:
        browser.close()
    assert calls == ["launch", "close"]
    with pytest.raises(RuntimeError) as second:
        browser.close()
    assert calls == ["launch", "close", "close"]
    assert second.value is first.value
    if failed_step == "close":
        assert first.value is error
    else:
        assert first.value is not error
        assert str(first.value) == "secondary close failure"
