from __future__ import annotations

import importlib
import json
import os
import stat
import sys
from typing import TYPE_CHECKING, Any

import pytest
from analytics_toolkit import general, sql
from analytics_toolkit.sql.connection.errors import SqlConfigError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

config_module = importlib.import_module("analytics_toolkit.sql.connection.config")
references_module = importlib.import_module("analytics_toolkit.sql.connection.references")
secret_file_module = importlib.import_module("analytics_toolkit.sql.connection.secret_file")
secret_setup_module = importlib.import_module("analytics_toolkit.sql.connection.secret_setup")


def _write_secrets(path: Path, text: str, *, mode: int = 0o600) -> Path:
    secrets_path = path / ".secrets"
    secrets_path.write_text(text, encoding="utf-8")
    secrets_path.chmod(mode)
    return secrets_path


def test_secret_references_resolve_scalar_and_json_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    bundle = json.dumps(
        {
            "credentials": {"password": "secret-password"},
            "settings": {"connect_timeout": "500", "max_threads": 4},
        },
        separators=(",", ":"),
    )
    _write_secrets(
        tmp_path,
        f"export CH_BUNDLE='{bundle}'\nexport CH_HOST='secret-ch.example'\n",
    )
    write_sql_connections(
        {
            "ch_secret": {
                "type": "ch",
                "host": {"from": ".secrets", "key": "CH_HOST"},
                "user": "ch-user",
                "password": {
                    "from": ".secrets",
                    "key": "CH_BUNDLE",
                    "path": ["credentials", "password"],
                },
                "settings": {
                    "from": ".secrets",
                    "key": "CH_BUNDLE",
                    "path": ["settings"],
                },
            }
        }
    )
    original_loader = references_module.load_secret_values
    load_count = 0

    def load_once() -> tuple[Path, dict[str, str]]:
        nonlocal load_count
        load_count += 1
        return original_loader()

    monkeypatch.setattr(references_module, "load_secret_values", load_once)

    config = config_module.get_connection_config("ch_secret")

    assert config.host == "secret-ch.example"
    assert config.password == "secret-password"
    assert config.settings == {"connect_timeout": "500", "max_threads": 4}
    assert "secret-password" not in repr(config)
    assert load_count == 1


def test_secret_resolution_uses_selected_connections_directory(
    tmp_path: Path,
) -> None:
    selected_dir = tmp_path / "selected"
    selected_dir.mkdir()
    selected_connections = selected_dir / ".connections"
    selected_connections.write_text(
        json.dumps(
            {
                "gp": {
                    "type": "gp",
                    "host": "gp.example",
                    "user": "user",
                    "password": {"from": ".secrets", "key": "GP_PASSWORD"},
                    "database": "db",
                }
            }
        ),
        encoding="utf-8",
    )
    _write_secrets(tmp_path, "export GP_PASSWORD='wrong'\n")
    _write_secrets(selected_dir, "export GP_PASSWORD='selected-secret'\n")
    general.set_connections_path(selected_connections)

    try:
        config = config_module.get_connection_config("gp")
    finally:
        general.set_connections_path(None)

    assert config.password == "selected-secret"


@pytest.mark.parametrize(
    ("secret_text", "message"),
    [
        (None, "Missing SQL secrets file"),
        ("export OTHER='value'\n", "is not set"),
        ("export GP_PASSWORD=''\n", "is empty"),
    ],
)
def test_secret_reference_reports_missing_file_key_and_empty_value(
    tmp_path: Path,
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
    secret_text: str | None,
    message: str,
) -> None:
    write_sql_connections(
        {
            "gp": {
                "type": "gp",
                "host": "gp.example",
                "user": "user",
                "password": {"from": ".secrets", "key": "GP_PASSWORD"},
                "database": "db",
            }
        }
    )
    if secret_text is not None:
        _write_secrets(tmp_path, secret_text)

    with pytest.raises(SqlConfigError, match=message) as caught:
        config_module.get_connection_config("gp")

    assert "value" not in str(caught.value)


@pytest.mark.parametrize(
    ("secret_text", "message"),
    [
        ("GP_PASSWORD = 'value'\n", "no spaces around"),
        ('export GP_PASSWORD="value"\n', "must use"),
        ("export GP_PASSWORD=\n", "must use"),
        ("export GP_PASSWORD=$(command)\n", "must use"),
        ("export GP_PASSWORD='unterminated\n", "must use"),
        ("export GP_PASSWORD='bad\x00value'\n", "must use"),
        (
            "export GP_PASSWORD='first'\nGP_PASSWORD='second'\n",
            "duplicate key",
        ),
    ],
)
def test_secret_file_rejects_unsafe_or_ambiguous_assignments(
    tmp_path: Path,
    secret_text: str,
    message: str,
) -> None:
    _write_secrets(tmp_path, secret_text)

    with pytest.raises(SqlConfigError, match=message) as caught:
        secret_file_module.load_secret_values()

    assert "first" not in str(caught.value)
    assert "second" not in str(caught.value)


def test_secret_file_requires_utf8(tmp_path: Path) -> None:
    secrets_path = tmp_path / ".secrets"
    secrets_path.write_bytes(b"export SECRET='\xff'\n")
    secrets_path.chmod(0o600)

    with pytest.raises(SqlConfigError, match="UTF-8"):
        secret_file_module.load_secret_values()


def test_secret_file_requires_connections_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(secret_file_module, "find_connections_file_path", lambda: None)

    with pytest.raises(SqlConfigError, match="Missing SQL connections file"):
        secret_file_module.load_secret_values()


def test_set_missing_secrets_preserves_content_and_quotes_apostrophes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections(
        {
            "gp": {
                "type": "gp",
                "host": {"from": ".secrets", "key": "EXISTING_SECRET"},
                "user": {"from": ".secrets", "key": "MISSING_SECRET"},
                "password": {"from": ".secrets", "key": "EMPTY_SECRET"},
            },
            "trino": {
                "type": "trino",
                "password": {"from": ".secrets", "key": "MISSING_SECRET"},
                "request_timeout": {"from": "env", "key": "IGNORED_ENV"},
            },
        }
    )
    secrets_path = _write_secrets(
        tmp_path,
        "# Keep this comment\n"
        "export EXISTING_SECRET='already-set'\n"
        "export EMPTY_SECRET=''\n"
        "UNUSED_SECRET='keep-me'\n",
    )
    responses = iter(["missing'value", "", "empty-value"])
    prompts: list[str] = []

    def prompt(message: str) -> str:
        prompts.append(message)
        return next(responses)

    monkeypatch.setattr(secret_setup_module, "getpass", prompt)

    changed = sql.set_missing_secrets()

    assert changed == ["MISSING_SECRET", "EMPTY_SECRET"]
    assert prompts == [
        "Enter value for secret 'MISSING_SECRET': ",
        "Enter value for secret 'EMPTY_SECRET': ",
        "Enter value for secret 'EMPTY_SECRET': ",
    ]
    assert secrets_path.read_text(encoding="utf-8") == (
        "# Keep this comment\n"
        "export EXISTING_SECRET='already-set'\n"
        "export EMPTY_SECRET='empty-value'\n"
        "UNUSED_SECRET='keep-me'\n"
        "export MISSING_SECRET='missing'\\''value'\n"
    )
    _path, values = secret_file_module.load_secret_values()
    assert values["MISSING_SECRET"] == "missing'value"


def test_set_missing_secrets_creates_private_file_from_airflow_overrides(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    write_sql_connections: Callable[[dict[str, Any]], Path],
) -> None:
    write_sql_connections(
        {
            "source": "airflow",
            "connections": {
                "trino": {
                    "type": "trino",
                    "connection_id": "AirTrino",
                    "request_timeout": {
                        "from": ".secrets",
                        "key": "TRINO_REQUEST_TIMEOUT",
                    },
                    "password": {
                        "from": "airflow_variable",
                        "key": "TRINO_PASSWORD_AF",
                    },
                }
            },
        }
    )
    monkeypatch.setitem(sys.modules, "airflow", None)
    monkeypatch.setattr(secret_setup_module, "getpass", lambda _message: "900")

    assert sql.set_missing_secrets() == ["TRINO_REQUEST_TIMEOUT"]

    secrets_path = tmp_path / ".secrets"
    assert secrets_path.read_text(encoding="utf-8") == ("export TRINO_REQUEST_TIMEOUT='900'\n")
    if os.name == "posix":
        assert stat.S_IMODE(secrets_path.stat().st_mode) == 0o600


def test_set_missing_secrets_is_noop_without_secret_references(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        secret_setup_module,
        "getpass",
        lambda _message: pytest.fail("unexpected secret prompt"),
    )

    assert sql.set_missing_secrets() == []
    assert not (tmp_path / ".secrets").exists()


def test_set_missing_secrets_is_noop_when_referenced_values_exist(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections(
        {
            "gp": {
                "type": "gp",
                "password": {"from": ".secrets", "key": "GP_PASSWORD"},
            }
        }
    )
    secrets_path = _write_secrets(tmp_path, "export GP_PASSWORD='existing'\n")
    monkeypatch.setattr(
        secret_setup_module,
        "getpass",
        lambda _message: pytest.fail("existing secret was prompted"),
    )

    assert sql.set_missing_secrets() == []
    assert secrets_path.read_text(encoding="utf-8") == ("export GP_PASSWORD='existing'\n")


def test_set_missing_secrets_does_not_write_partial_values_on_interrupt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections(
        {
            "gp": {
                "type": "gp",
                "user": {"from": ".secrets", "key": "FIRST_SECRET"},
                "password": {"from": ".secrets", "key": "SECOND_SECRET"},
            }
        }
    )
    secrets_path = _write_secrets(tmp_path, "# unchanged\n")
    responses: list[str | BaseException] = ["first-value", KeyboardInterrupt()]

    def prompt(_message: str) -> str:
        response = responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    monkeypatch.setattr(secret_setup_module, "getpass", prompt)

    with pytest.raises(KeyboardInterrupt):
        sql.set_missing_secrets()

    assert secrets_path.read_text(encoding="utf-8") == "# unchanged\n"


def test_set_missing_secrets_rejects_concurrent_file_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections(
        {
            "gp": {
                "type": "gp",
                "password": {"from": ".secrets", "key": "GP_PASSWORD"},
            }
        }
    )
    secrets_path = _write_secrets(tmp_path, "export GP_PASSWORD=''\n")

    def prompt(_message: str) -> str:
        secrets_path.write_text("export GP_PASSWORD='external'\n", encoding="utf-8")
        return "prompted"

    monkeypatch.setattr(secret_setup_module, "getpass", prompt)

    with pytest.raises(SqlConfigError, match="changed while values were being entered"):
        sql.set_missing_secrets()

    assert secrets_path.read_text(encoding="utf-8") == ("export GP_PASSWORD='external'\n")


def test_set_missing_secrets_cleans_up_failed_atomic_replacement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections(
        {
            "gp": {
                "type": "gp",
                "password": {"from": ".secrets", "key": "GP_PASSWORD"},
            }
        }
    )
    secrets_path = _write_secrets(tmp_path, "export GP_PASSWORD=''\n")
    monkeypatch.setattr(secret_setup_module, "getpass", lambda _message: "secret")

    def fail_replace(_path: Path, _target: Path) -> None:
        message = "simulated replacement failure"
        raise OSError(message)

    monkeypatch.setattr(secret_file_module.Path, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated replacement failure"):
        sql.set_missing_secrets()

    assert secrets_path.read_text(encoding="utf-8") == "export GP_PASSWORD=''\n"
    assert list(tmp_path.glob(".secrets.*")) == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits required")
def test_secret_file_warns_but_preserves_insecure_permissions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections(
        {
            "gp": {
                "type": "gp",
                "password": {"from": ".secrets", "key": "GP_PASSWORD"},
            }
        }
    )
    secrets_path = _write_secrets(
        tmp_path,
        "export GP_PASSWORD=''\n",
        mode=0o644,
    )
    monkeypatch.setattr(secret_setup_module, "getpass", lambda _message: "secret")

    with pytest.warns(UserWarning, match="use 0600"):
        sql.set_missing_secrets()

    assert stat.S_IMODE(secrets_path.stat().st_mode) == 0o644


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("bad-name", "value"),
        ("GOOD", "x\n"),
        ("GOOD", "x\r"),
        ("GOOD", "x\x00"),
    ],
)
def test_secret_writer_rejects_invalid_keys_and_multiline_values(
    name: str,
    value: str,
) -> None:
    with pytest.raises(SqlConfigError):
        secret_file_module._quote_secret_value(name, value)
