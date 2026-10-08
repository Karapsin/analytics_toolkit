"""Credential diagnostics, client isolation, and shared organization pacing."""

import json
import subprocess
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from analytics_toolkit.datalens_utils.auth import client, credentials
from analytics_toolkit.datalens_utils.cli import run_cli
from analytics_toolkit.datalens_utils.errors import DataLensUtilsError
from datalens_sdk import APIErrorContext, NotFoundError


@pytest.mark.parametrize("interval", ["slow", True, -1, 61, float("nan"), float("inf")])
def test_invalid_interval_stops_before_credentials(session_state, interval):
    session_state.runtime["request_interval_seconds"] = interval
    with patch.object(client, "extract_credentials") as token, pytest.raises(
        DataLensUtilsError
    ), client.datalens_client():
        pass
    token.assert_not_called()


def test_pacing_is_shared_by_organization_and_keeps_client_counter(session_state):
    session_state.runtime["request_interval_seconds"] = 1.2
    client._NEXT_REQUEST.clear()
    sdk = MagicMock()
    with patch.object(client, "extract_credentials", return_value="offline-token"), patch.object(
        client, "DataLensClientYC", return_value=sdk
    ) as create, patch.object(client.time, "monotonic", return_value=10), patch.object(
        client.time, "sleep"
    ) as sleep:
        with client.datalens_client():
            first = create.call_args.kwargs["event_hooks"]["request"][0]
        with client.datalens_client():
            second = create.call_args.kwargs["event_hooks"]["request"][0]
        first(None)
        second(None)
        assert session_state.runtime["_request_count"] == 2
        sleep.assert_called_once()
        assert sleep.call_args.args[0] == pytest.approx(1.2)
    client._NEXT_REQUEST.clear()


def test_injected_client_never_authenticates(session_state):
    sentinel = object()
    session_state.client_factory = lambda state: nullcontext(sentinel)
    with patch.object(client, "extract_credentials") as token, client.datalens_client() as actual:
        assert actual is sentinel
    token.assert_not_called()


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (SimpleNamespace(returncode=0, stdout=json.dumps({"iam_token": "offline"})), "offline"),
        (SimpleNamespace(returncode=1, stdout="secret output"), "Could not obtain"),
        (SimpleNamespace(returncode=0, stdout="not json"), "unexpected"),
        (SimpleNamespace(returncode=0, stdout="{}"), "unexpected"),
    ],
)
def test_credentials_parse_without_saving_or_echoing_token(session_state, result, expected):
    with patch.object(credentials, "yc_binary", return_value="/existing/yc"), patch.object(
        credentials.subprocess, "run", return_value=result
    ) as run:
        if expected == "offline":
            assert credentials.extract_credentials() == "offline"
        else:
            with pytest.raises(DataLensUtilsError, match=expected) as caught:
                credentials.extract_credentials()
            assert "secret output" not in str(caught.value)
    assert run.call_args.args[0][-1] == "profile"
    assert not session_state.paths.runtime_root.exists()


def test_credentials_timeout(session_state):
    with patch.object(credentials, "yc_binary", return_value="/existing/yc"), patch.object(
        credentials.subprocess, "run", side_effect=subprocess.TimeoutExpired("yc", 30)
    ), pytest.raises(DataLensUtilsError, match="Timed out"):
        credentials.extract_credentials()


def test_cli_preserves_unexpected_errors_and_formats_sdk_failure(capsys):
    project = MagicMock()
    project._execute.side_effect = RuntimeError("unexpected")
    with pytest.raises(RuntimeError, match="unexpected"):
        run_cli(project)
    project._execute.side_effect = NotFoundError(
        APIErrorContext(404, "NOT_FOUND", "missing", request_id="offline-request")
    )
    assert run_cli(project) == 1
    assert "offline-request" in capsys.readouterr().err
