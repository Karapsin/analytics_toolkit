from __future__ import annotations

from typing import Any

import pytest
from analytics_toolkit.sql.backends.retry_logging import safe_error_code

from tests.sql._support.retries import retry_module
from tests.sql._support.retry_logging import (
    legacy_clickhouse_error,
    marked_trino_error,
    trusted_native_error,
)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("errorName", "PRIVATE_AUTH_TOKEN"),
        ("errorName", "TOKEN"),
        ("errorName", "22P02"),
        ("errorName", "PRIVATE_AUTH_TOKEN\nPASSWORD=private-value"),
        ("errorName", "PRIVATE_AUTH_TOKEN" * 100),
        ("errorCode", "PRIVATE_AUTH_TOKEN"),
        ("errorCode", "PRIVATE_AUTH_TOKEN\nPASSWORD=private-value"),
        ("errorCode", "PRIVATE_AUTH_TOKEN" * 100),
        ("errorCode", 10**150),
        ("errorCode", "9" * 150),
        ("errorCode", True),
    ],
)
def test_safe_stop_log_omits_untrusted_marked_trino_metadata(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    field: str,
    value: Any,
) -> None:
    error = marked_trino_error(field, value)
    attempts: list[int] = []
    sleeps: list[float] = []
    monkeypatch.setattr(retry_module.time, "sleep", sleeps.append)

    def fail(attempt: int) -> None:
        attempts.append(attempt)
        raise error

    with pytest.raises(type(error)) as caught:
        retry_module.run_with_retry("staged operation", 3, 5, fail, safe_exception_logging=True)

    assert caught.value is error
    assert attempts == [1]
    assert sleeps == []
    output = capsys.readouterr().out
    assert "reason=explicit_terminal_marker" in output
    assert "code=" not in output
    assert "PRIVATE_AUTH_TOKEN" not in output
    assert "private-value" not in output
    assert "private exception details" not in output


@pytest.mark.parametrize(
    ("backend", "code"), [("ch", "516"), ("gp", "22P02"), ("trino", "TYPE_MISMATCH")]
)
def test_safe_stop_log_retains_trusted_native_codes(
    capsys: pytest.CaptureFixture[str], backend: str, code: str
) -> None:
    error = trusted_native_error(backend)

    def fail(_attempt: int) -> None:
        raise error

    with pytest.raises(type(error)):
        retry_module.run_with_retry("native operation", 1, 0, fail, safe_exception_logging=True)

    output = capsys.readouterr().out
    assert f"code={code}" in output
    assert "private exception details" not in output


@pytest.mark.parametrize("code", [0, 123456, 4294967295, "123456"])
def test_safe_stop_log_retains_bounded_numeric_codes(
    capsys: pytest.CaptureFixture[str], code: int | str
) -> None:
    error = marked_trino_error("errorCode", code)

    def fail(_attempt: int) -> None:
        raise error

    with pytest.raises(type(error)):
        retry_module.run_with_retry("native operation", 1, 0, fail, safe_exception_logging=True)

    assert f"code={code}" in capsys.readouterr().out


def test_ordinary_stop_log_retains_existing_native_metadata_rendering(
    capsys: pytest.CaptureFixture[str],
) -> None:
    error = marked_trino_error("errorName", "PRIVATE_AUTH_TOKEN")

    def fail(_attempt: int) -> None:
        raise error

    with pytest.raises(type(error)):
        retry_module.run_with_retry("ordinary operation", 1, 0, fail)

    assert "code=PRIVATE_AUTH_TOKEN" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("backend", "code"),
    [
        ("trino", -1),
        ("trino", "4294967296"),
        ("trino", "99999999999"),
        ("gp", "22p02"),
        ("gp", "22P02\n"),
        ("gp", "PRIVATE_AUTH_TOKEN"),
        ("trino", "秘密"),
        ("unrecognized", "TYPE_MISMATCH"),
        (None, "TYPE_MISMATCH"),
        (None, None),
    ],
)
def test_safe_code_formatter_rejects_invalid_bounds_or_missing_backend_trust(
    backend: str | None, code: str | int | None
) -> None:
    assert safe_error_code(backend, code) is None


@pytest.mark.parametrize(
    "name",
    [
        "ALREADY_EXISTS",
        "COLUMN_NOT_FOUND",
        "FEATURE_NOT_SUPPORTED",
        "FUNCTION_NOT_FOUND",
        "INSUFFICIENT_PRIVILEGE",
        "SCHEMA_NOT_FOUND",
        "TABLE_NOT_FOUND",
    ],
)
def test_safe_stop_log_retains_recognized_clickhouse_legacy_names(
    capsys: pytest.CaptureFixture[str], name: str
) -> None:
    error = legacy_clickhouse_error(name)

    def fail(_attempt: int) -> None:
        raise error

    with pytest.raises(type(error)):
        retry_module.run_with_retry("native operation", 1, 0, fail, safe_exception_logging=True)

    output = capsys.readouterr().out
    assert f"code={name}" in output
    assert "private exception details" not in output
