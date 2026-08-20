from __future__ import annotations

import builtins
import importlib
import json
import os
import subprocess
import sys
import types
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import analytics_toolkit.general as general_module
import pandas as pd
import pytest
from analytics_toolkit.sql.connection.errors import (
    InvalidSqlInputError,
    SqlConfigError,
    UnsupportedConnectionTypeError,
)
from analytics_toolkit.sql.ddl.models import CreateSqlTableOptions
from analytics_toolkit.sql.execution.plans import SqlOperationMetadata

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

config_module = importlib.import_module("analytics_toolkit.sql.connection.config")
config_path_module = importlib.import_module(
    "analytics_toolkit.sql.connection.config_path"
)
_resolve_calling_base_dir = config_path_module._resolve_calling_base_dir
connections_state_module = importlib.import_module(
    "analytics_toolkit.general.connections"
)
connection_module = importlib.import_module("analytics_toolkit.sql.connection.get_sql_connection")
api_module = importlib.import_module("analytics_toolkit.sql.dml.transfer.flow.api")
create_sql_table_module = importlib.import_module("analytics_toolkit.sql.ddl.api")
target_replace_module = importlib.import_module("analytics_toolkit.sql.ddl.target_replace")
operation_runner_module = importlib.import_module(
    "analytics_toolkit.sql.execution.operation_runner"
)
transfer_schema_module = importlib.import_module("analytics_toolkit.sql.dml.transfer.schema")
load_sql_table_module = importlib.import_module("analytics_toolkit.sql.dml.load.load_sql_table")
trino_config_module = importlib.import_module("analytics_toolkit.sql.backends.trino.config")
ch_config_module = importlib.import_module("analytics_toolkit.sql.backends.ch.config")

_MISSING = object()


def _fake_drop_target_sqls(*_args: object, **_kwargs: object) -> list[str]:
    return ["drop target;"]


def test_sql_facade_import_is_airflow_parse_safe(tmp_path: Path) -> None:
    script = r"""
import builtins
import importlib

blocked_roots = {"airflow", "clickhouse_connect", "clickhouse_driver", "psycopg2", "trino"}
real_import = builtins.__import__


def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    root = name.split(".", 1)[0]
    if level == 0 and root in blocked_roots:
        raise AssertionError(f"unexpected runtime import: {name}")
    return real_import(name, globals, locals, fromlist, level)


builtins.__import__ = guarded_import
sql = importlib.import_module("analytics_toolkit.sql")
assert callable(sql.read)
assert callable(sql.execute)
assert not hasattr(sql, "read_sql")
assert not hasattr(sql, "execute_sql")
assert not hasattr(sql, "transfer_table")
assert callable(sql.airflow_query_label)
"""
    env = dict(os.environ)
    repo_root = Path(__file__).resolve().parents[1]
    env["PYTHONPATH"] = (
        f"{repo_root}{os.pathsep}{env['PYTHONPATH']}" if env.get("PYTHONPATH") else str(repo_root)
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


class FakeAirflowConnection:
    def __init__(
        self,
        *,
        conn_type: str | None = None,
        host: str | None = None,
        port: int | None = None,
        login: str | None = None,
        password: str | None = None,
        schema: str | None = None,
        extra_dejson: dict[str, object] | object = _MISSING,
        extra: str | dict[str, object] | None = None,
    ) -> None:
        self.conn_type = conn_type
        self.host = host
        self.port = port
        self.login = login
        self.password = password
        self.schema = schema
        self.extra = extra
        if extra_dejson is not _MISSING:
            self.extra_dejson = extra_dejson


def install_fake_airflow(
    monkeypatch: pytest.MonkeyPatch,
    connections: dict[str, FakeAirflowConnection],
    variables: dict[str, str] | None = None,
) -> None:
    airflow_variables = variables or {}

    class FakeBaseHook:
        @staticmethod
        def get_connection(connection_id: str) -> FakeAirflowConnection:
            try:
                return connections[connection_id]
            except KeyError as exc:
                raise KeyError(connection_id) from exc

    class FakeVariable:
        @staticmethod
        def get(name: str) -> str:
            try:
                return airflow_variables[name]
            except KeyError as exc:
                raise KeyError(name) from exc

    airflow_module = types.ModuleType("airflow")
    hooks_module = types.ModuleType("airflow.hooks")
    base_module = types.ModuleType("airflow.hooks.base")
    models_module = types.ModuleType("airflow.models")
    variable_module = types.ModuleType("airflow.models.variable")
    base_module.BaseHook = FakeBaseHook
    variable_module.Variable = FakeVariable
    models_module.Variable = FakeVariable
    models_module.variable = variable_module
    airflow_module.hooks = hooks_module
    airflow_module.models = models_module
    hooks_module.base = base_module
    monkeypatch.setitem(sys.modules, "airflow", airflow_module)
    monkeypatch.setitem(sys.modules, "airflow.hooks", hooks_module)
    monkeypatch.setitem(sys.modules, "airflow.hooks.base", base_module)
    monkeypatch.setitem(sys.modules, "airflow.models", models_module)
    monkeypatch.setitem(sys.modules, "airflow.models.variable", variable_module)


def _write_cert(relative_path: str, contents: str = "CERT\n") -> Path:
    path = Path.cwd() / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")
    return path


def _install_fake_trino(
    monkeypatch: pytest.MonkeyPatch,
    connect_calls: list[dict[str, object]],
) -> None:
    class FakeBasicAuthentication:
        def __init__(self, user: str, password: str | None) -> None:
            self.user = user
            self.password = password

    fake_auth = types.ModuleType("trino.auth")
    fake_auth.BasicAuthentication = FakeBasicAuthentication
    fake_auth.OAuth2Authentication = lambda: object()
    fake_trino = types.ModuleType("trino")
    fake_trino.auth = fake_auth
    fake_trino.dbapi = types.SimpleNamespace(
        connect=lambda **kwargs: connect_calls.append(kwargs) or object()
    )
    monkeypatch.setitem(sys.modules, "trino", fake_trino)
    monkeypatch.setitem(sys.modules, "trino.auth", fake_auth)


def test_connections_file_lookup_searches_from_cwd_to_parents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_dir = tmp_path / "project"
    runtime_dir = project_dir / "dags" / "task"
    runtime_dir.mkdir(parents=True)
    connections_path = project_dir / ".connections"
    connections_path.write_text(
        json.dumps(
            {
                "parent_gp": {
                    "type": "gp",
                    "host": "parent-gp.example",
                    "user": "user",
                    "password": "password",
                    "database": "db",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(runtime_dir)

    config = config_module.get_connection_config("parent_gp")

    assert config_module.get_connections_file_path() == connections_path
    assert config.host == "parent-gp.example"


def test_connections_file_lookup_prefers_calling_script_to_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script_root = tmp_path / "airflow_project"
    script_dir = script_root / "dags" / "tasks"
    script_dir.mkdir(parents=True)
    script_connections = script_root / ".connections"
    script_connections.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        config_path_module, "_resolve_calling_base_dir", lambda: script_dir
    )

    general_module.set_connections_path(None)

    assert config_module.get_connections_file_path() == script_connections.resolve()


def test_connections_path_caller_resolver_reuses_general_path_logic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_read_file_module = types.SimpleNamespace(_resolve_base_dir=lambda: tmp_path)
    monkeypatch.setattr(
        config_path_module,
        "import_module",
        lambda _name: fake_read_file_module,
    )

    assert _resolve_calling_base_dir() == tmp_path


def test_connections_path_search_directories_are_deduplicated(tmp_path: Path) -> None:
    nested_dir = tmp_path / "project" / "dags"
    search_directories = list(
        config_path_module._iter_search_directories(
            [nested_dir, nested_dir.parent]
        )
    )

    assert search_directories.count(nested_dir.parent.resolve()) == 1


def test_connections_file_lookup_recovers_from_remembered_directory_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    old_runtime_dir = project_root / "z" / "T"
    old_runtime_dir.mkdir(parents=True)
    old_connections = old_runtime_dir / ".connections"
    old_connections.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config_path_module, "_resolve_calling_base_dir", lambda: None)
    monkeypatch.chdir(old_runtime_dir)

    general_module.set_connections_path(None)
    assert config_module.get_connections_file_path() == old_connections.resolve()

    old_connections.unlink()
    recovered_connections = old_runtime_dir.parent / ".connections"
    recovered_connections.write_text("{}", encoding="utf-8")
    unrelated_runtime = tmp_path / "worker" / "runtime"
    unrelated_runtime.mkdir(parents=True)
    monkeypatch.chdir(unrelated_runtime)

    assert config_module.get_connections_file_path() == recovered_connections.resolve()
    assert (
        connections_state_module.get_last_connections_path()
        == recovered_connections.resolve()
    )


def test_missing_explicit_connections_path_recovers_and_promotes_caller_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".connections").unlink()
    old_runtime_dir = tmp_path / "old_worker" / "T"
    old_runtime_dir.mkdir(parents=True)
    old_connections = old_runtime_dir / ".connections"
    old_connections.write_text("{}", encoding="utf-8")
    script_root = tmp_path / "airflow_project"
    script_dir = script_root / "dags" / "tasks"
    script_dir.mkdir(parents=True)
    script_connections = script_root / ".connections"
    script_connections.write_text("{}", encoding="utf-8")
    cwd_root = tmp_path / "worker"
    cwd_dir = cwd_root / "runtime"
    cwd_dir.mkdir(parents=True)
    (cwd_root / ".connections").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        config_path_module, "_resolve_calling_base_dir", lambda: script_dir
    )
    monkeypatch.chdir(cwd_dir)

    general_module.set_connections_path(old_connections)
    old_connections.unlink()

    assert config_module.get_connections_file_path() == script_connections.resolve()
    assert (
        connections_state_module.get_connections_path_override()
        == script_connections.resolve()
    )


def test_recovered_connections_path_anchors_relative_certificates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".connections").unlink()
    project_root = tmp_path / "airflow_project"
    old_runtime_dir = project_root / "rotated" / "T"
    old_runtime_dir.mkdir(parents=True)
    old_connections = old_runtime_dir / ".connections"
    old_connections.write_text("{}", encoding="utf-8")
    recovered_connections = project_root / ".connections"
    recovered_connections.write_text(
        json.dumps(
            {
                "gp_ssl": {
                    "type": "gp",
                    "host": "gp.example",
                    "user": "user",
                    "password": "password",
                    "database": "db",
                    "ca_certs": "gp-ca.pem",
                }
            }
        ),
        encoding="utf-8",
    )
    certs_dir = project_root / ".certs"
    certs_dir.mkdir()
    ca_path = certs_dir / "gp-ca.pem"
    ca_path.write_text("GP CA\n", encoding="utf-8")
    connect_calls: list[dict[str, object]] = []
    monkeypatch.setitem(
        sys.modules,
        "psycopg2",
        types.SimpleNamespace(
            connect=lambda **kwargs: connect_calls.append(kwargs) or object()
        ),
    )
    monkeypatch.setattr(config_path_module, "_resolve_calling_base_dir", lambda: None)

    general_module.set_connections_path(old_connections)
    old_connections.unlink()
    connection_module.get_sql_connection("gp_ssl")

    assert connect_calls[0]["sslrootcert"] == str(ca_path.resolve())
    assert (
        connections_state_module.get_connections_path_override()
        == recovered_connections.resolve()
    )


def test_recovered_invalid_connections_file_does_not_fall_through(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".connections").unlink()
    old_runtime_dir = tmp_path / "old_worker" / "T"
    old_runtime_dir.mkdir(parents=True)
    old_connections = old_runtime_dir / ".connections"
    old_connections.write_text("{}", encoding="utf-8")
    recovered_connections = old_runtime_dir.parent / ".connections"
    recovered_connections.write_text("{not json", encoding="utf-8")
    caller_dir = tmp_path / "airflow_project" / "dags"
    caller_dir.mkdir(parents=True)
    (caller_dir.parent / ".connections").write_text(
        json.dumps({"gp": {"type": "gp"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        config_path_module, "_resolve_calling_base_dir", lambda: caller_dir
    )

    general_module.set_connections_path(old_connections)
    old_connections.unlink()

    with pytest.raises(SqlConfigError, match="must contain valid JSON"):
        config_module.get_connection_config("gp")

    assert (
        connections_state_module.get_connections_path_override()
        == recovered_connections.resolve()
    )


def test_set_connections_path_override_wins_over_cwd_connections(
    tmp_path: Path,
) -> None:
    override_dir = tmp_path / "airflow_project"
    override_dir.mkdir()
    override_path = override_dir / ".connections"
    override_path.write_text(
        json.dumps(
            {
                "override_gp": {
                    "type": "gp",
                    "host": "override-gp.example",
                    "user": "user",
                    "password": "password",
                    "database": "db",
                }
            }
        ),
        encoding="utf-8",
    )

    try:
        selected_path = general_module.set_connections_path(override_path)
        config = config_module.get_connection_config("override_gp")

        assert selected_path == override_path.resolve()
        assert config_module.get_connections_file_path() == override_path.resolve()
        assert config.host == "override-gp.example"
    finally:
        general_module.set_connections_path(None)


def test_set_connections_path_none_restores_default_lookup(tmp_path: Path) -> None:
    override_dir = tmp_path / "airflow_project"
    override_dir.mkdir()
    override_path = override_dir / ".connections"
    override_path.write_text(
        json.dumps(
            {
                "override_gp": {
                    "type": "gp",
                    "host": "override-gp.example",
                    "user": "user",
                    "password": "password",
                    "database": "db",
                }
            }
        ),
        encoding="utf-8",
    )

    general_module.set_connections_path(override_path)
    reset_path = general_module.set_connections_path(None)

    assert reset_path is None
    assert connections_state_module.get_connections_path_override() is None
    assert connections_state_module.get_last_connections_path() is None
    assert config_module.get_connections_file_path() == tmp_path / ".connections"
    assert config_module.get_connection_config("gp").host == "gp.example"


@pytest.mark.parametrize(
    "path_factory",
    [
        lambda root: root / "connections.json",
        lambda root: root / "missing" / ".connections",
        lambda root: root / "directory" / ".connections",
    ],
)
def test_set_connections_path_rejects_invalid_paths(
    tmp_path: Path,
    path_factory: Callable[[Path], Path],
) -> None:
    invalid_path = path_factory(tmp_path)
    if invalid_path.name == "connections.json":
        invalid_path.write_text("{}", encoding="utf-8")
    elif invalid_path.parent.name == "directory":
        invalid_path.mkdir(parents=True)

    with pytest.raises(ValueError):
        general_module.set_connections_path(invalid_path)

    assert general_module.set_connections_path(None) is None


def test_set_connections_path_uses_selected_directory_for_certificates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    override_dir = tmp_path / "airflow_project"
    certs_dir = override_dir / ".certs"
    certs_dir.mkdir(parents=True)
    ca_path = certs_dir / "gp-ca.pem"
    ca_path.write_text("GP CA\n", encoding="utf-8")
    override_path = override_dir / ".connections"
    override_path.write_text(
        json.dumps(
            {
                "gp_ssl": {
                    "type": "gp",
                    "host": "override-gp.example",
                    "user": "user",
                    "password": "password",
                    "database": "db",
                    "ca_certs": "gp-ca.pem",
                }
            }
        ),
        encoding="utf-8",
    )
    connect_calls: list[dict[str, object]] = []
    fake_psycopg2 = types.SimpleNamespace(
        connect=lambda **kwargs: connect_calls.append(kwargs) or object()
    )
    monkeypatch.setitem(sys.modules, "psycopg2", fake_psycopg2)

    try:
        general_module.set_connections_path(override_path)
        connection_module.get_sql_connection("gp_ssl")

        assert connect_calls[0]["sslrootcert"] == str(ca_path.resolve())
    finally:
        general_module.set_connections_path(None)


def test_connection_alias_resolves_backend() -> None:
    config = config_module.get_connection_config("gp_sandbox")

    assert config.connection_key == "gp_sandbox"
    assert config.backend == "gp"
    assert config.database == "sandbox"
    assert config.connect_timeout == 30
    assert config.keepalives is True
    assert config.keepalives_idle == 60
    assert config.keepalives_interval == 10
    assert config.keepalives_count == 3


def test_gp_connection_uses_liveness_defaults(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    connect_calls: list[dict[str, object]] = []
    fake_psycopg2 = types.SimpleNamespace(
        connect=lambda **kwargs: connect_calls.append(kwargs) or object()
    )
    monkeypatch.setitem(sys.modules, "psycopg2", fake_psycopg2)

    connection_module.get_sql_connection("gp")

    output = capsys.readouterr().out
    assert "[get_sql_connection] [gp/gp] [connect] Opening connection" in output
    assert connect_calls == [
        {
            "host": "gp.example",
            "port": 5432,
            "user": "user",
            "password": "password",
            "dbname": "db",
            "connect_timeout": 30,
            "keepalives": 1,
            "keepalives_idle": 60,
            "keepalives_interval": 10,
            "keepalives_count": 3,
        }
    ]


def test_gp_connection_liveness_options_can_be_overridden(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections(
        {
            "gp_custom": {
                "type": "gp",
                "host": "gp.example",
                "port": 5432,
                "user": "user",
                "password": "password",
                "database": "db",
                "connect_timeout": "7",
                "keepalives": "false",
                "keepalives_idle": "8",
                "keepalives_interval": 9,
                "keepalives_count": 4,
            }
        }
    )
    connect_calls: list[dict[str, object]] = []
    fake_psycopg2 = types.SimpleNamespace(
        connect=lambda **kwargs: connect_calls.append(kwargs) or object()
    )
    monkeypatch.setitem(sys.modules, "psycopg2", fake_psycopg2)

    config = config_module.get_connection_config("gp_custom")
    connection_module.get_sql_connection("gp_custom")

    assert config.connect_timeout == 7
    assert config.keepalives is False
    assert config.keepalives_idle == 8
    assert config.keepalives_interval == 9
    assert config.keepalives_count == 4
    assert connect_calls == [
        {
            "host": "gp.example",
            "port": 5432,
            "user": "user",
            "password": "password",
            "dbname": "db",
            "connect_timeout": 7,
            "keepalives": 0,
        }
    ]


def test_greenplum_connection_passes_ssl_cert_options(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    ca_path = _write_cert(".certs/gp-ca.pem", "GP CA\n")
    client_cert = _write_cert(".certs/client.pem", "CLIENT CERT\n")
    client_key = _write_cert(".certs/client.key", "CLIENT KEY\n")
    write_sql_connections(
        {
            "gp_ssl": {
                "type": "gp",
                "host": "gp.example",
                "user": "user",
                "password": "password",
                "database": "db",
                "ca_certs": "gp-ca.pem",
                "ssl_cert": ".certs/client.pem",
                "ssl_key": ".certs/client.key",
            }
        }
    )
    connect_calls: list[dict[str, object]] = []
    fake_psycopg2 = types.SimpleNamespace(
        connect=lambda **kwargs: connect_calls.append(kwargs) or object()
    )
    monkeypatch.setitem(sys.modules, "psycopg2", fake_psycopg2)

    config = config_module.get_connection_config("gp_ssl")
    connection_module.get_sql_connection("gp_ssl")

    assert config.sslmode == "verify-full"
    assert config.ca_certs == ["gp-ca.pem"]
    assert connect_calls[0]["sslmode"] == "verify-full"
    assert connect_calls[0]["sslrootcert"] == str(ca_path.resolve())
    assert connect_calls[0]["sslcert"] == str(client_cert.resolve())
    assert connect_calls[0]["sslkey"] == str(client_key.resolve())


def test_greenplum_connection_respects_explicit_sslmode(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    _write_cert(".certs/gp-ca.pem")
    write_sql_connections(
        {
            "gp_ssl": {
                "type": "gp",
                "host": "gp.example",
                "user": "user",
                "password": "password",
                "database": "db",
                "sslmode": "verify-ca",
                "ca_certs": "gp-ca.pem",
            }
        }
    )
    connect_calls: list[dict[str, object]] = []
    fake_psycopg2 = types.SimpleNamespace(
        connect=lambda **kwargs: connect_calls.append(kwargs) or object()
    )
    monkeypatch.setitem(sys.modules, "psycopg2", fake_psycopg2)

    connection_module.get_sql_connection("gp_ssl")

    assert connect_calls[0]["sslmode"] == "verify-ca"


def test_greenplum_ca_certs_missing_file_fails_only_when_connecting(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections(
        {
            "gp_ssl": {
                "type": "gp",
                "host": "gp.example",
                "user": "user",
                "password": "password",
                "database": "db",
                "ca_certs": "missing.pem",
            }
        }
    )
    connect_calls: list[dict[str, object]] = []
    fake_psycopg2 = types.SimpleNamespace(
        connect=lambda **kwargs: connect_calls.append(kwargs) or object()
    )
    monkeypatch.setitem(sys.modules, "psycopg2", fake_psycopg2)

    config = config_module.get_connection_config("gp_ssl")
    assert config.ca_certs == ["missing.pem"]
    with pytest.raises(config_module.SqlConfigError, match="missing certificate file"):
        connection_module.get_sql_connection("gp_ssl")
    assert connect_calls == []


def test_trino_ca_certs_resolves_bare_name_and_overrides_verify(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    ca_path = _write_cert(".certs/trino-ca.pem", "TRINO CA\n")
    write_sql_connections(
        {
            "trino_ssl": {
                "type": "trino",
                "host": "trino.example",
                "user": "user",
                "verify": False,
                "ca_certs": "trino-ca.pem",
            }
        }
    )
    connect_calls: list[dict[str, object]] = []
    _install_fake_trino(monkeypatch, connect_calls)

    config = config_module.get_connection_config("trino_ssl")
    connection_module.get_sql_connection("trino_ssl")

    assert config.verify_value == "false"
    assert config.ca_certs == ["trino-ca.pem"]
    assert connect_calls[0]["verify"] == str(ca_path.resolve())


def test_trino_ca_certs_missing_file_fails_only_when_connecting(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections(
        {
            "trino_ssl": {
                "type": "trino",
                "host": "trino.example",
                "user": "user",
                "ca_certs": "missing.pem",
            }
        }
    )
    connect_calls: list[dict[str, object]] = []
    _install_fake_trino(monkeypatch, connect_calls)

    config = config_module.get_connection_config("trino_ssl")
    assert config.ca_certs == ["missing.pem"]
    with pytest.raises(config_module.SqlConfigError, match="missing certificate file"):
        connection_module.get_sql_connection("trino_ssl")
    assert connect_calls == []


def test_multiple_ca_certs_are_bundled_in_order(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    _write_cert(".certs/root.pem", "ROOT\n")
    _write_cert(".certs/intermediate.pem", "INTERMEDIATE\n")
    write_sql_connections(
        {
            "trino_bundle": {
                "type": "trino",
                "host": "trino.example",
                "user": "user",
                "ca_certs": ["root.pem", ".certs/intermediate.pem"],
            }
        }
    )
    connect_calls: list[dict[str, object]] = []
    _install_fake_trino(monkeypatch, connect_calls)

    connection_module.get_sql_connection("trino_bundle")

    bundle_path = Path(str(connect_calls[0]["verify"]))
    expected_bundle_path = Path.cwd() / ".certs" / ".generated" / "trino_bundle-ca-bundle.pem"
    assert bundle_path == expected_bundle_path
    assert bundle_path.read_text(encoding="utf-8") == "ROOT\nINTERMEDIATE\n"


def test_clickhouse_ca_certs_list_uses_generated_bundle(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    _write_cert(".certs/clickhouse-root.pem", "CLICKHOUSE ROOT\n")
    _write_cert(".certs/clickhouse-intermediate.pem", "CLICKHOUSE INTERMEDIATE\n")
    write_sql_connections(
        {
            "ch_ssl": {
                "type": "ch",
                "host": "ch.example",
                "user": "user",
                "password": "password",
                "ca_certs": ["clickhouse-root.pem", "clickhouse-intermediate.pem"],
            }
        }
    )
    client = object()
    client_calls: list[dict[str, object]] = []
    fake_clickhouse_connect = types.SimpleNamespace(
        common=types.SimpleNamespace(set_setting=lambda name, value: None),
        get_client=lambda **kwargs: client_calls.append(kwargs) or client,
    )
    monkeypatch.setitem(sys.modules, "clickhouse_connect", fake_clickhouse_connect)
    monkeypatch.setitem(
        sys.modules,
        "clickhouse_connect.common",
        fake_clickhouse_connect.common,
    )

    result = connection_module.get_sql_connection("ch_ssl")

    assert result is client
    bundle_path = Path(str(client_calls[0]["ca_cert"]))
    expected_bundle_path = Path.cwd() / ".certs" / ".generated" / "ch_ssl-ca-bundle.pem"
    assert bundle_path == expected_bundle_path
    assert bundle_path.read_text(encoding="utf-8") == "CLICKHOUSE ROOT\nCLICKHOUSE INTERMEDIATE\n"


def test_clickhouse_ca_certs_missing_file_fails_only_when_connecting(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections(
        {
            "ch_ssl": {
                "type": "ch",
                "host": "ch.example",
                "user": "user",
                "password": "password",
                "ca_certs": "missing.pem",
            }
        }
    )
    client_calls: list[dict[str, object]] = []
    fake_clickhouse_connect = types.SimpleNamespace(
        common=types.SimpleNamespace(set_setting=lambda name, value: None),
        get_client=lambda **kwargs: client_calls.append(kwargs) or object(),
    )
    monkeypatch.setitem(sys.modules, "clickhouse_connect", fake_clickhouse_connect)
    monkeypatch.setitem(
        sys.modules,
        "clickhouse_connect.common",
        fake_clickhouse_connect.common,
    )

    config = config_module.get_connection_config("ch_ssl")
    assert config.ca_certs == ["missing.pem"]
    with pytest.raises(config_module.SqlConfigError, match="missing certificate file"):
        connection_module.get_sql_connection("ch_ssl")
    assert client_calls == []


def test_clickhouse_connection_disables_auto_session_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = object()
    setting_calls: list[tuple[str, object]] = []
    client_calls: list[dict[str, object]] = []

    fake_clickhouse_connect = types.SimpleNamespace(
        common=types.SimpleNamespace(
            set_setting=lambda name, value: setting_calls.append((name, value))
        ),
        get_client=lambda **kwargs: client_calls.append(kwargs) or client,
    )
    monkeypatch.setitem(sys.modules, "clickhouse_connect", fake_clickhouse_connect)
    monkeypatch.setitem(
        sys.modules,
        "clickhouse_connect.common",
        fake_clickhouse_connect.common,
    )

    result = connection_module.get_sql_connection("ch")

    assert result is client
    assert setting_calls == [("autogenerate_session_id", False)]
    assert client_calls == [
        {
            "host": "ch.example",
            "port": 8123,
            "username": "user",
            "password": "password",
            "secure": False,
            "database": "default",
        }
    ]


def test_unknown_connection_key_raises_config_error() -> None:
    with pytest.raises(config_module.UnsupportedConnectionTypeError):
        config_module.get_connection_config("missing")


def test_malformed_connections_file_raises_config_error(tmp_path: Path) -> None:
    (tmp_path / ".connections").write_text("{not json", encoding="utf-8")

    with pytest.raises(config_module.SqlConfigError):
        config_module.get_connection_config("gp")


@pytest.mark.parametrize(
    ("connection_key", "raw_config"),
    [
        ("trino_keychain", {"type": "trino", "use_keychain_certs": True}),
        ("trino_keychain_names", {"type": "trino", "keychain_cert_names": ["ca"]}),
        ("trino_ca_cert", {"type": "trino", "ca_cert": "trino-ca.pem"}),
        ("gp_ca_cert", {"type": "gp", "ca_cert": "gp-ca.pem"}),
        ("ch_ca_cert", {"type": "ch", "ca_cert": "clickhouse-ca.pem"}),
        ("ch_ca_cert_variable", {"type": "ch", "ca_cert_variable": "clickhouse_ca"}),
    ],
)
def test_removed_certificate_fields_raise_config_error(
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
    connection_key: str,
    raw_config: dict[str, object],
) -> None:
    write_sql_connections({connection_key: raw_config})

    with pytest.raises(config_module.SqlConfigError, match="not supported"):
        config_module.get_connection_config(connection_key)


def test_generate_dummy_connections_writes_direct_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / ".connections").unlink()

    created = config_module.generate_dummy_connections()

    assert created == tmp_path / ".connections"
    assert (tmp_path / ".certs").is_dir()
    assert created.read_text(encoding="utf-8").endswith("\n")
    assert json.loads(created.read_text(encoding="utf-8")) == {
        "gp": {
            "type": "gp",
            "host": "gp.example",
            "port": 5432,
            "user": "user",
            "password": "password",
            "database": "db",
            "ca_certs": "gp-ca.pem",
            "ddl_defaults": {
                "regular": {
                    "appendonly": True,
                    "blocksize": 32768,
                    "compresstype": "zstd",
                    "compresslevel": 4,
                    "orientation": "column",
                },
                "staging": {},
            },
        },
        "trino": {
            "type": "trino",
            "host": "trino.example",
            "port": 8080,
            "user": "user",
            "password": "password",
            "catalog": "iceberg",
            "schema": "sandbox",
            "http_scheme": "https",
            "ca_certs": "trino-ca.pem",
            "transfer_staging_schema": "object_storage.sandbox",
            "s3_transfer_staging_schema": "object_storage.sandbox_s3",
            "s3_transfer_staging_location": "s3://bucket/tmp/analytics_toolkit_transfer",
            "aws_access_key_id": "object-storage-access-key",
            "aws_secret_access_key": "object-storage-secret-key",
            "aws_endpoint_url": "https://storage.example",
            "upsert_partition_drop_sql_template": (
                "ALTER TABLE {table} DROP PARTITION ({partition_column} = {partition_value})"
            ),
            "ddl_defaults": {
                "regular": {"format": "'PARQUET'", "object_store_layout_enabled": True},
                "staging": {},
                "parquet_staging": {},
            },
        },
        "ch": {
            "type": "ch",
            "host": "ch.example",
            "port": 8123,
            "user": "user",
            "password": "password",
            "database": "default",
            "secure": True,
            "ca_certs": "clickhouse-ca.pem",
            "ddl_defaults": config_module._dummy_ch_ddl_defaults(),
        },
    }
    output = capsys.readouterr().out
    assert ".certs" in output
    assert "Greenplum" in output
    assert "Trino" in output
    assert "ClickHouse" in output
    assert config_module.load_sql_connections()["gp"]["type"] == "gp"


def test_generate_dummy_connections_writes_airflow_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / ".connections").unlink()

    created = config_module.generate_dummy_connections(airflow=True)

    assert created == tmp_path / ".connections"
    assert (tmp_path / ".certs").is_dir()
    assert created.read_text(encoding="utf-8").endswith("\n")
    assert json.loads(created.read_text(encoding="utf-8")) == {
        "source": "airflow",
        "connections": {
            "gp": {
                "type": "gp",
                "ca_certs": "gp-ca.pem",
                "ddl_defaults": {
                    "regular": {
                        "appendonly": True,
                        "blocksize": 32768,
                        "compresstype": "zstd",
                        "compresslevel": 4,
                        "orientation": "column",
                    },
                    "staging": {},
                },
            },
            "trino": {
                "type": "trino",
                "ca_certs": "trino-ca.pem",
                "transfer_staging_schema": "object_storage.sandbox",
                "s3_transfer_staging_schema": "object_storage.sandbox_s3",
                "s3_transfer_staging_location": "s3://bucket/tmp/analytics_toolkit_transfer",
                "endpoint_url": "https://storage.example",
                "upsert_partition_drop_sql_template": (
                    "ALTER TABLE {table} DROP PARTITION ({partition_column} = {partition_value})"
                ),
                "ddl_defaults": {
                    "regular": {"format": "'PARQUET'", "object_store_layout_enabled": True},
                    "staging": {},
                    "parquet_staging": {},
                },
            },
            "ch": {
                "type": "ch",
                "ca_certs": "clickhouse-ca.pem",
                "ddl_defaults": config_module._dummy_ch_ddl_defaults(),
            },
        },
    }
    output = capsys.readouterr().out
    assert ".certs" in output
    assert "Greenplum" in output
    assert "Trino" in output
    assert "ClickHouse" in output
    dummy_distributed = config_module._dummy_ch_ddl_defaults()["regular"]["distributed"]
    assert dummy_distributed["cluster"] == "CORE"


def test_generate_dummy_connections_rejects_existing_file(tmp_path: Path) -> None:
    connections_path = tmp_path / ".connections"
    original_content = connections_path.read_text(encoding="utf-8")

    with pytest.raises(ValueError, match="SQL connections file already exists"):
        config_module.generate_dummy_connections()

    assert connections_path.read_text(encoding="utf-8") == original_content


def test_missing_connections_file_ignores_legacy_backend_env_vars(
    monkeypatch,
    tmp_path: Path,
) -> None:
    (tmp_path / ".connections").unlink()
    monkeypatch.setenv("GP_HOST", "legacy-host")
    monkeypatch.setenv("GP_USER", "legacy-user")
    monkeypatch.setenv("GP_PASSWORD", "legacy-password")
    monkeypatch.setenv("GP_DATABASE", "legacy-db")
    monkeypatch.setenv(
        "SQL_CONNECTIONS",
        (
            '{"gp":{"type":"gp","host":"legacy","user":"legacy",'
            '"password":"legacy","database":"legacy"}}'
        ),
    )

    with pytest.raises(config_module.SqlConfigError, match=r"\.connections"):
        config_module.get_connection_config("gp")


def test_airflow_gp_connection_maps_airflow_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_airflow(
        monkeypatch,
        {
            "AirGp": FakeAirflowConnection(
                conn_type="postgres",
                host="air-gp.example",
                port=15432,
                login="air-user",
                password="air-password",
                schema="air_db",
                extra_dejson={
                    "connect_timeout": "9",
                    "keepalives": "false",
                    "keepalives_idle": "20",
                    "keepalives_interval": 5,
                    "keepalives_count": "2",
                },
            )
        },
    )

    config = config_module.airflow_connection_config("AirGp")

    assert isinstance(config, config_module.GpConfig)
    assert config.connection_key == "AirGp"
    assert config.backend == "gp"
    assert config.host == "air-gp.example"
    assert config.port == 15432
    assert config.user == "air-user"
    assert config.password == "air-password"
    assert config.database == "air_db"
    assert config.connect_timeout == 9
    assert config.keepalives is False
    assert config.keepalives_idle == 20
    assert config.keepalives_interval == 5
    assert config.keepalives_count == 2


def test_airflow_trino_connection_maps_extras(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_airflow(
        monkeypatch,
        {
            "AirTrino": FakeAirflowConnection(
                conn_type="trino",
                host="air-trino.example",
                port=8443,
                login="trino-user",
                password="trino-password",
                extra_dejson={
                    "catalog": "iceberg",
                    "schema": "sandbox",
                    "auth_mode": "basic",
                    "http_scheme": "https",
                    "verify": False,
                    "insert_chunk_size": "500",
                    "request_timeout": "700",
                    "source": "airflow",
                },
            )
        },
    )

    config = config_module.airflow_connection_config("AirTrino")

    assert isinstance(config, config_module.TrinoConfig)
    assert config.connection_key == "AirTrino"
    assert config.backend == "trino"
    assert config.host == "air-trino.example"
    assert config.port == 8443
    assert config.user == "trino-user"
    assert config.password == "trino-password"
    assert config.catalog == "iceberg"
    assert config.schema == "sandbox"
    assert config.auth_mode == "basic"
    assert config.http_scheme == "https"
    assert config.verify_value == "false"
    assert config.insert_chunk_size == 500
    assert config.request_timeout == 700
    assert config.source == "airflow"


def test_airflow_clickhouse_connection_maps_airflow_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_airflow(
        monkeypatch,
        {
            "AirCh": FakeAirflowConnection(
                conn_type="clickhouse",
                host="air-ch.example",
                port=9440,
                login="ch-user",
                password="ch-password",
                schema="default",
                extra_dejson={
                    "secure": "true",
                    "verify": False,
                    "ca_certs_variable": "clickhouse_ca_cert",
                    "connect_timeout": "11",
                    "send_receive_timeout": "6001",
                    "settings": {"use_numpy": True},
                    "interface": "https",
                    "query_limit": "100",
                    "query_retries": "4",
                    "client_name": "analytics-toolkit",
                },
            )
        },
    )

    config = config_module.airflow_connection_config("AirCh")

    assert isinstance(config, config_module.ChConfig)
    assert config.connection_key == "AirCh"
    assert config.backend == "ch"
    assert config.host == "air-ch.example"
    assert config.port == 9440
    assert config.user == "ch-user"
    assert config.password == "ch-password"
    assert config.database == "default"
    assert config.secure is True
    assert config.verify_value == "false"
    assert config.ca_certs == []
    assert config.ca_certs_variable == "clickhouse_ca_cert"
    assert config.connect_timeout == 11
    assert config.send_receive_timeout == 6001
    assert config.settings == {"use_numpy": True}
    assert config.interface == "https"
    assert config.query_limit == 100
    assert config.query_retries == 4
    assert config.client_name == "analytics-toolkit"


def test_airflow_clickhouse_connection_uses_dag_compatible_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_airflow(
        monkeypatch,
        {
            "AirCh": FakeAirflowConnection(
                conn_type="clickhouse",
                host="air-ch.example",
                login="ch-user",
                password="ch-password",
                schema="default",
            )
        },
    )

    config = config_module.airflow_connection_config("AirCh")

    assert isinstance(config, config_module.ChConfig)
    assert config.send_receive_timeout == 6000
    assert config.settings == {"connect_timeout": "500"}


def test_clickhouse_driver_defaults_select_transport_ports(
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections(
        {
            "http": {
                "type": "ch",
                "host": "http.example",
                "user": "user",
                "password": "password",
            },
            "native": {
                "type": "ch",
                "driver": "native",
                "host": "native.example",
                "user": "user",
                "password": "password",
            },
        }
    )

    http = config_module.get_connection_config("http")
    native = config_module.get_connection_config("native")

    assert isinstance(http, config_module.ChConfig)
    assert http.driver == "http"
    assert http.port == 8123
    assert http.compression is False
    assert isinstance(native, config_module.ChConfig)
    assert native.driver == "native"
    assert native.port == 9000
    assert native.compression is False


def test_airflow_source_native_clickhouse_keeps_explicit_port_and_options(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, object]], Path],
) -> None:
    write_sql_connections(
        {
            "source": "airflow",
            "connections": {
                "clickhouse_pa_core": {
                    "type": "ch",
                    "driver": "native",
                    "compression": {"from": "extra", "fallback": False},
                    "ca_certs_variable": "ca_certificate",
                    "send_receive_timeout": 6000,
                    "settings": {"connect_timeout": "500"},
                }
            },
        }
    )
    install_fake_airflow(
        monkeypatch,
        {
            "clickhouse_pa_core": FakeAirflowConnection(
                conn_type="clickhouse",
                host="native.example",
                port=9003,
                login="native-user",
                password="native-password",
                schema="analytics",
                extra_dejson={"compression": "lz4"},
            )
        },
    )

    config = config_module.get_connection_config("clickhouse_pa_core")

    assert isinstance(config, config_module.ChConfig)
    assert config.driver == "native"
    assert config.port == 9003
    assert config.compression == "lz4"
    assert config.settings == {"connect_timeout": "500"}


@pytest.mark.parametrize("field", ["interface", "query_limit", "query_retries"])
def test_native_clickhouse_rejects_http_only_fields(
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
    field: str,
) -> None:
    write_sql_connections(
        {
            "native_bad": {
                "type": "ch",
                "driver": "native",
                "host": "native.example",
                "user": "user",
                "password": "password",
                field: 1,
            }
        }
    )

    with pytest.raises(SqlConfigError, match=r"native_bad.*HTTP-only"):
        config_module.get_connection_config("native_bad")


def test_invalid_clickhouse_driver_is_connection_specific(
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections(
        {
            "bad_driver": {
                "type": "ch",
                "driver": "tcp",
                "host": "native.example",
                "user": "user",
                "password": "password",
            }
        }
    )

    with pytest.raises(SqlConfigError, match=r"bad_driver.*driver.*http.*native"):
        config_module.get_connection_config("bad_driver")


@pytest.mark.parametrize("compression", [1, "gzip"])
def test_invalid_clickhouse_compression_is_connection_specific(
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
    compression: object,
) -> None:
    write_sql_connections(
        {
            "bad_compression": {
                "type": "ch",
                "driver": "native",
                "host": "native.example",
                "user": "user",
                "password": "password",
                "compression": compression,
            }
        }
    )

    with pytest.raises(SqlConfigError, match=r"bad_compression.*compression"):
        config_module.get_connection_config("bad_compression")


def test_native_clickhouse_constructor_and_lazy_airflow_ca(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    ca_path = _write_cert(".certs/native-ca.pem")
    write_sql_connections(
        {
            "native": {
                "type": "ch",
                "driver": "native",
                "host": "native.example",
                "port": 9003,
                "user": "native-user",
                "password": "native-password",
                "database": "analytics",
                "secure": True,
                "verify": False,
                "ca_certs_variable": "ca_certificate",
                "connect_timeout": 11,
                "send_receive_timeout": 6000,
                "settings": {"connect_timeout": "500"},
                "compression": "zstd",
                "client_name": "analytics-toolkit",
            }
        }
    )
    install_fake_airflow(monkeypatch, {}, variables={"ca_certificate": "native-ca.pem"})
    calls: list[dict[str, object]] = []

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            calls.append(kwargs)

    fake_module = types.ModuleType("clickhouse_driver")
    fake_module.Client = FakeClient
    monkeypatch.setitem(sys.modules, "clickhouse_driver", fake_module)

    config = config_module.get_connection_config("native")
    assert isinstance(config, config_module.ChConfig)
    assert calls == []
    result = connection_module.get_sql_connection("native")

    assert result.__class__.__name__ == "NativeClickHouseClient"
    assert calls == [
        {
            "host": "native.example",
            "port": 9003,
            "user": "native-user",
            "password": "native-password",
            "database": "analytics",
            "secure": True,
            "verify": False,
            "ca_certs": str(ca_path.resolve()),
            "connect_timeout": 11,
            "send_receive_timeout": 6000,
            "settings": {"connect_timeout": "500"},
            "compression": "zstd",
            "client_name": "analytics-toolkit",
        }
    ]


def test_native_clickhouse_constructor_omits_unconfigured_options(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections(
        {
            "native": {
                "type": "ch",
                "driver": "native",
                "host": "native.example",
                "user": "user",
                "password": "password",
            }
        }
    )
    calls: list[dict[str, object]] = []

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            calls.append(kwargs)

    fake_module = types.ModuleType("clickhouse_driver")
    fake_module.Client = FakeClient
    monkeypatch.setitem(sys.modules, "clickhouse_driver", fake_module)

    connection_module.get_sql_connection("native")

    assert calls == [
        {
            "host": "native.example",
            "port": 9000,
            "user": "user",
            "password": "password",
            "database": "",
            "secure": False,
            "compression": False,
        }
    ]


def test_missing_native_clickhouse_dependency_has_installation_hint(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections(
        {
            "native": {
                "type": "ch",
                "driver": "native",
                "host": "native.example",
                "user": "user",
                "password": "password",
            }
        }
    )
    real_import = builtins.__import__

    def blocked_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "clickhouse_driver" or name.startswith("clickhouse_driver."):
            message = "blocked"
            raise ImportError(message)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    monkeypatch.delitem(sys.modules, "clickhouse_driver", raising=False)

    with pytest.raises(ImportError, match="Native ClickHouse") as caught:
        connection_module.get_sql_connection("native")
    assert "Install analytics-toolkit[clickhouse-native]." in str(caught.value)


def test_airflow_clickhouse_file_overrides_airflow_extras(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, object]], Path],
) -> None:
    write_sql_connections(
        {
            "source": "airflow",
            "connections": {
                "AirCh": {
                    "type": "ch",
                    "send_receive_timeout": 12,
                    "settings": {"max_threads": 4},
                    "ca_certs": "/local/ca.pem",
                }
            },
        }
    )
    install_fake_airflow(
        monkeypatch,
        {
            "AirCh": FakeAirflowConnection(
                conn_type="clickhouse",
                host="air-ch.example",
                login="ch-user",
                password="ch-password",
                schema="default",
                extra_dejson={
                    "send_receive_timeout": 6000,
                    "settings": {"connect_timeout": "500"},
                    "ca_certs_variable": "clickhouse_ca_cert",
                },
            )
        },
    )

    config = config_module.get_connection_config("AirCh")

    assert isinstance(config, config_module.ChConfig)
    assert config.send_receive_timeout == 12
    assert config.settings == {"max_threads": 4}
    assert config.ca_certs == ["/local/ca.pem"]
    assert config.ca_certs_variable is None


def test_clickhouse_connection_passes_optional_connector_settings(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    certs_dir = Path.cwd() / ".certs"
    certs_dir.mkdir()
    ca_path = certs_dir / "clickhouse-ca.pem"
    ca_path.write_text("CLICKHOUSE CA\n", encoding="utf-8")
    write_sql_connections(
        {
            "ch_custom": {
                "type": "ch",
                "host": "ch.example",
                "port": 8123,
                "user": "user",
                "password": "password",
                "database": "default",
                "secure": True,
                "verify": "false",
                "ca_certs_variable": "clickhouse_ca_cert",
                "connect_timeout": "11",
                "send_receive_timeout": "6001",
                "settings": {"connect_timeout": "500", "use_numpy": True},
                "interface": "https",
                "query_limit": "100",
                "query_retries": "4",
                "ddl_ready_timeout_seconds": "900",
                "ddl_ready_timeout_extension_cnt": "4",
                "client_name": "analytics-toolkit",
            }
        }
    )
    install_fake_airflow(
        monkeypatch,
        {},
        variables={"clickhouse_ca_cert": "clickhouse-ca.pem"},
    )
    client = object()
    client_calls: list[dict[str, object]] = []
    fake_clickhouse_connect = types.SimpleNamespace(
        common=types.SimpleNamespace(set_setting=lambda name, value: None),
        get_client=lambda **kwargs: client_calls.append(kwargs) or client,
    )
    monkeypatch.setitem(sys.modules, "clickhouse_connect", fake_clickhouse_connect)
    monkeypatch.setitem(
        sys.modules,
        "clickhouse_connect.common",
        fake_clickhouse_connect.common,
    )

    result = connection_module.get_sql_connection("ch_custom")

    assert result is client
    assert config_module.get_connection_config("ch_custom").ddl_ready_timeout_seconds == 900
    assert (
        config_module.get_connection_config("ch_custom").ddl_ready_timeout_extension_cnt == 4
    )
    assert client_calls == [
        {
            "host": "ch.example",
            "port": 8123,
            "username": "user",
            "password": "password",
            "secure": True,
            "database": "default",
            "verify": False,
            "ca_cert": str(ca_path.resolve()),
            "connect_timeout": 11,
            "send_receive_timeout": 6001,
            "settings": {"connect_timeout": "500", "use_numpy": True},
            "interface": "https",
            "query_limit": 100,
            "query_retries": 4,
            "client_name": "analytics-toolkit",
        }
    ]


def test_clickhouse_settings_must_be_mapping(
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections(
        {
            "ch_bad": {
                "type": "ch",
                "host": "ch.example",
                "user": "user",
                "password": "password",
                "settings": ["use_numpy"],
            }
        }
    )

    with pytest.raises(config_module.SqlConfigError, match="settings"):
        config_module.get_connection_config("ch_bad")


def test_clickhouse_missing_ca_certs_variable_is_clear_config_error(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections(
        {
            "ch_custom": {
                "type": "ch",
                "host": "ch.example",
                "user": "user",
                "password": "password",
                "ca_certs_variable": "missing_certificate",
            }
        }
    )
    install_fake_airflow(monkeypatch, {}, variables={})
    fake_clickhouse_connect = types.SimpleNamespace(
        common=types.SimpleNamespace(set_setting=lambda name, value: None),
        get_client=lambda **kwargs: object(),
    )
    monkeypatch.setitem(sys.modules, "clickhouse_connect", fake_clickhouse_connect)
    monkeypatch.setitem(
        sys.modules,
        "clickhouse_connect.common",
        fake_clickhouse_connect.common,
    )

    with pytest.raises(
        config_module.SqlConfigError,
        match="Could not resolve Airflow Variable 'missing_certificate'",
    ):
        connection_module.get_sql_connection("ch_custom")


def test_airflow_connections_are_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_airflow(
        monkeypatch,
        {
            "gp": FakeAirflowConnection(
                conn_type="postgres",
                host="air-gp.example",
                login="air-user",
                password="air-password",
                schema="air_db",
            )
        },
    )

    default_config = config_module.get_connection_config("gp")
    with config_module.use_airflow_connections():
        airflow_config = config_module.get_connection_config("gp")
    restored_config = config_module.get_connection_config("gp")

    assert default_config.host == "gp.example"
    assert isinstance(airflow_config, config_module.GpConfig)
    assert airflow_config.host == "air-gp.example"
    assert restored_config.host == "gp.example"


def test_airflow_context_resolves_public_sql_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_airflow(
        monkeypatch,
        {
            "AirGp": FakeAirflowConnection(
                host="air-gp.example",
                port=15432,
                login="air-user",
                password="air-password",
                schema="air_db",
            )
        },
    )
    connect_calls: list[dict[str, object]] = []
    fake_psycopg2 = types.SimpleNamespace(
        connect=lambda **kwargs: connect_calls.append(kwargs) or object()
    )
    monkeypatch.setitem(sys.modules, "psycopg2", fake_psycopg2)

    with config_module.use_airflow_connections({"AirGp": "gp"}):
        connection_module.get_sql_connection("AirGp")

    assert connect_calls == [
        {
            "host": "air-gp.example",
            "port": 15432,
            "user": "air-user",
            "password": "air-password",
            "dbname": "air_db",
            "connect_timeout": 30,
            "keepalives": 1,
            "keepalives_idle": 60,
            "keepalives_interval": 10,
            "keepalives_count": 3,
        }
    ]


def test_transfer_options_use_airflow_context_alias_backends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_airflow(
        monkeypatch,
        {
            "AirTrino": FakeAirflowConnection(
                conn_type="trino",
                host="air-trino.example",
                login="trino-user",
                extra_dejson={"catalog": "iceberg", "schema": "sandbox"},
            ),
            "AirGp": FakeAirflowConnection(
                conn_type="postgres",
                host="air-gp.example",
                login="air-user",
                password="air-password",
                schema="air_db",
            ),
        },
    )

    with config_module.use_airflow_connections():
        options = api_module.build_transfer_options(
            from_db="AirTrino",
            to_db="AirGp",
            from_sql="select 1",
            to_table="schema.target",
            gp_distributed_by_key=["id"],
        )

    assert options.from_db_key == "AirTrino"
    assert options.from_db_backend == "trino"
    assert options.to_db_key == "AirGp"
    assert options.to_db_backend == "gp"
    assert options.gp_distributed_by_key == ["id"]


def test_unknown_airflow_connection_id_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_airflow(monkeypatch, {})

    with pytest.raises(
        config_module.UnsupportedConnectionTypeError,
        match="Unknown Airflow connection ID: missing",
    ):
        config_module.airflow_connection_config("missing", "gp")


def test_airflow_connection_config_requires_airflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for module_name in ("airflow.hooks.base", "airflow.hooks", "airflow"):
        monkeypatch.delitem(sys.modules, module_name, raising=False)

    real_import = builtins.__import__

    def fake_import(
        name: str,
        globals_: object | None = None,
        locals_: object | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        if name.startswith("airflow"):
            message = "airflow is unavailable"
            raise ImportError(message)
        return real_import(name, globals_, locals_, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(config_module.SqlConfigError, match="apache-airflow"):
        config_module.airflow_connection_config("missing", "gp")


def test_airflow_connections_file_resolves_alias_without_context(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, object]], Path],
) -> None:
    write_sql_connections(
        {
            "source": "airflow",
            "connections": {
                "AirGp": {
                    "type": "gp",
                    "connect_timeout": "8",
                }
            },
        }
    )
    install_fake_airflow(
        monkeypatch,
        {
            "AirGp": FakeAirflowConnection(
                conn_type="postgres",
                host="air-gp.example",
                port=15432,
                login="air-user",
                password="air-password",
                schema="air_db",
            )
        },
    )

    config = config_module.get_connection_config("AirGp")

    assert isinstance(config, config_module.GpConfig)
    assert config.connection_key == "airgp"
    assert config.backend == "gp"
    assert config.host == "air-gp.example"
    assert config.port == 15432
    assert config.user == "air-user"
    assert config.password == "air-password"
    assert config.database == "air_db"
    assert config.connect_timeout == 8


def test_airflow_connections_file_allows_alias_different_from_connection_id(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, object]], Path],
) -> None:
    write_sql_connections(
        {
            "source": "airflow",
            "connections": {
                "trino": {
                    "connection_id": "AirTrino",
                    "type": "trino",
                    "insert_chunk_size": 400,
                }
            },
        }
    )
    install_fake_airflow(
        monkeypatch,
        {
            "AirTrino": FakeAirflowConnection(
                conn_type="trino",
                host="air-trino.example",
                port=8443,
                login="trino-user",
                password="trino-password",
                extra_dejson={"catalog": "iceberg", "schema": "sandbox"},
            )
        },
    )

    config = config_module.get_connection_config("trino")
    raw_connections = config_module.load_sql_connections()

    assert isinstance(config, config_module.TrinoConfig)
    assert config.connection_key == "trino"
    assert config.host == "air-trino.example"
    assert config.catalog == "iceberg"
    assert config.schema == "sandbox"
    assert config.insert_chunk_size == 400
    assert raw_connections["trino"]["host"] == "air-trino.example"
    assert raw_connections["trino"]["insert_chunk_size"] == 400


def test_airflow_connections_file_supports_public_transfer_options(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, object]], Path],
) -> None:
    write_sql_connections(
        {
            "source": "airflow",
            "connections": {
                "airflow_trino": {"type": "trino"},
                "airflow_gp": {"type": "gp"},
            },
        }
    )
    install_fake_airflow(
        monkeypatch,
        {
            "airflow_trino": FakeAirflowConnection(
                conn_type="trino",
                host="air-trino.example",
                login="trino-user",
                extra_dejson={"catalog": "iceberg", "schema": "sandbox"},
            ),
            "airflow_gp": FakeAirflowConnection(
                conn_type="postgres",
                host="air-gp.example",
                login="air-user",
                password="air-password",
                schema="air_db",
            ),
        },
    )

    options = api_module.build_transfer_options(
        from_db="airflow_trino",
        to_db="airflow_gp",
        from_sql="select 1",
        to_table="schema.target",
        gp_distributed_by_key=["id"],
    )

    assert options.from_db_key == "airflow_trino"
    assert options.from_db_backend == "trino"
    assert options.to_db_key == "airflow_gp"
    assert options.to_db_backend == "gp"
    assert options.gp_distributed_by_key == ["id"]


def test_airflow_connections_file_rejects_missing_type(
    write_sql_connections: Callable[[dict[str, object]], Path],
) -> None:
    write_sql_connections(
        {
            "source": "airflow",
            "connections": {"gp": {}},
        }
    )

    with pytest.raises(config_module.SqlConfigError, match="type"):
        config_module.get_connection_config("gp")


def test_airflow_connections_file_rejects_unknown_source(
    write_sql_connections: Callable[[dict[str, object]], Path],
) -> None:
    write_sql_connections(
        {
            "source": "vault",
            "connections": {},
        }
    )

    with pytest.raises(config_module.SqlConfigError, match="unsupported"):
        config_module.get_connection_config("gp")


def test_airflow_connections_file_rejects_malformed_connections(
    write_sql_connections: Callable[[dict[str, object]], Path],
) -> None:
    write_sql_connections(
        {
            "source": "airflow",
            "connections": [],
        }
    )

    with pytest.raises(config_module.SqlConfigError, match="connections"):
        config_module.get_connection_config("gp")


def test_airflow_connections_file_unknown_airflow_id_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, object]], Path],
) -> None:
    write_sql_connections(
        {
            "source": "airflow",
            "connections": {"gp": {"type": "gp"}},
        }
    )
    install_fake_airflow(monkeypatch, {})

    with pytest.raises(
        config_module.UnsupportedConnectionTypeError,
        match="Unknown Airflow connection ID: gp",
    ):
        config_module.get_connection_config("gp")


def test_direct_connections_file_can_still_have_source_alias(
    write_sql_connections: Callable[[dict[str, object]], Path],
) -> None:
    write_sql_connections(
        {
            "source": {
                "type": "gp",
                "host": "gp-source.example",
                "user": "user",
                "password": "password",
                "database": "db",
            }
        }
    )

    config = config_module.get_connection_config("source")

    assert isinstance(config, config_module.GpConfig)
    assert config.host == "gp-source.example"


def test_trino_connection_uses_airflow_file_timeout_and_source_overrides(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, object]], Path],
) -> None:
    write_sql_connections(
        {
            "source": "airflow",
            "connections": {
                "trino": {
                    "connection_id": "AirTrino",
                    "type": "trino",
                    "request_timeout": "900",
                    "source": "analytics_toolkit",
                }
            },
        }
    )
    install_fake_airflow(
        monkeypatch,
        {
            "AirTrino": FakeAirflowConnection(
                conn_type="trino",
                host="air-trino.example",
                port=8443,
                login="trino-user",
                password="trino-password",
                extra_dejson={
                    "catalog": "iceberg",
                    "schema": "sandbox",
                    "http_scheme": "https",
                    "verify": False,
                    "request_timeout": 600,
                    "source": "airflow",
                },
            )
        },
    )
    connect_calls: list[dict[str, object]] = []

    class FakeBasicAuthentication:
        def __init__(self, user: str, password: str | None) -> None:
            self.user = user
            self.password = password

    fake_auth = types.ModuleType("trino.auth")
    fake_auth.BasicAuthentication = FakeBasicAuthentication
    fake_auth.OAuth2Authentication = lambda: object()
    fake_trino = types.ModuleType("trino")
    fake_trino.auth = fake_auth
    fake_trino.dbapi = types.SimpleNamespace(
        connect=lambda **kwargs: connect_calls.append(kwargs) or object()
    )
    monkeypatch.setitem(sys.modules, "trino", fake_trino)
    monkeypatch.setitem(sys.modules, "trino.auth", fake_auth)

    connection_module.get_sql_connection("trino")

    assert connect_calls == [
        {
            "host": "air-trino.example",
            "port": 8443,
            "user": "trino-user",
            "http_scheme": "https",
            "auth": connect_calls[0]["auth"],
            "verify": False,
            "catalog": "iceberg",
            "schema": "sandbox",
            "request_timeout": 900,
            "source": "analytics_toolkit",
        }
    ]
    auth = connect_calls[0]["auth"]
    assert isinstance(auth, FakeBasicAuthentication)
    assert auth.user == "trino-user"
    assert auth.password == "trino-password"


def test_airflow_file_extra_fallback_uses_airflow_extra_when_present(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, object]], Path],
) -> None:
    write_sql_connections(
        {
            "source": "airflow",
            "connections": {
                "trino": {
                    "connection_id": "AirTrino",
                    "type": "trino",
                    "http_scheme": {"from": "extra", "fallback": "https"},
                    "verify": {"from": "extra", "fallback": False},
                    "request_timeout": {"from": "extra", "fallback": 300},
                    "source": {
                        "from": "extra",
                        "key": "client_source",
                        "fallback": "airflow-trino",
                    },
                }
            },
        }
    )
    install_fake_airflow(
        monkeypatch,
        {
            "AirTrino": FakeAirflowConnection(
                conn_type="trino",
                host="air-trino.example",
                port=8443,
                login="trino-user",
                password="trino-password",
                extra_dejson={
                    "catalog": "iceberg",
                    "schema": "sandbox",
                    "http_scheme": "http",
                    "verify": True,
                    "request_timeout": 120,
                    "client_source": "airflow-extra",
                },
            )
        },
    )

    config = config_module.get_connection_config("trino")

    assert isinstance(config, config_module.TrinoConfig)
    assert config.http_scheme == "http"
    assert config.verify_value == "true"
    assert config.request_timeout == 120
    assert config.source == "airflow-extra"


def test_airflow_file_extra_fallback_uses_fallback_when_extra_missing(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, object]], Path],
) -> None:
    write_sql_connections(
        {
            "source": "airflow",
            "connections": {
                "trino": {
                    "connection_id": "AirTrino",
                    "type": "trino",
                    "http_scheme": {"from": "extra", "fallback": "https"},
                    "verify": {"from": "extra", "fallback": False},
                    "request_timeout": {"from": "extra", "fallback": 300},
                    "source": {"from": "extra", "fallback": "airflow-trino"},
                }
            },
        }
    )
    install_fake_airflow(
        monkeypatch,
        {
            "AirTrino": FakeAirflowConnection(
                conn_type="trino",
                host="air-trino.example",
                port=8443,
                login="trino-user",
                password="trino-password",
                extra_dejson={"catalog": "iceberg", "schema": "sandbox"},
            )
        },
    )
    connect_calls: list[dict[str, object]] = []

    class FakeBasicAuthentication:
        def __init__(self, user: str, password: str | None) -> None:
            self.user = user
            self.password = password

    fake_auth = types.ModuleType("trino.auth")
    fake_auth.BasicAuthentication = FakeBasicAuthentication
    fake_auth.OAuth2Authentication = lambda: object()
    fake_trino = types.ModuleType("trino")
    fake_trino.auth = fake_auth
    fake_trino.dbapi = types.SimpleNamespace(
        connect=lambda **kwargs: connect_calls.append(kwargs) or object()
    )
    monkeypatch.setitem(sys.modules, "trino", fake_trino)
    monkeypatch.setitem(sys.modules, "trino.auth", fake_auth)

    connection_module.get_sql_connection("trino")

    assert connect_calls[0]["http_scheme"] == "https"
    assert connect_calls[0]["verify"] is False
    assert connect_calls[0]["request_timeout"] == 300
    assert connect_calls[0]["source"] == "airflow-trino"


@pytest.mark.parametrize(
    "resolver",
    [
        {"from": "env", "fallback": "https"},
        {"from": "extra", "key": 123, "fallback": "https"},
        {"from": "extra", "fallback": "https", "unexpected": True},
        {"from": "extra", "default": "https"},
    ],
)
def test_airflow_file_extra_fallback_rejects_malformed_resolver(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, object]], Path],
    resolver: dict[str, object],
) -> None:
    write_sql_connections(
        {
            "source": "airflow",
            "connections": {
                "trino": {
                    "connection_id": "AirTrino",
                    "type": "trino",
                    "http_scheme": resolver,
                }
            },
        }
    )
    install_fake_airflow(
        monkeypatch,
        {
            "AirTrino": FakeAirflowConnection(
                conn_type="trino",
                host="air-trino.example",
                login="trino-user",
            )
        },
    )

    with pytest.raises(config_module.SqlConfigError, match="resolver"):
        config_module.get_connection_config("trino")


def test_direct_connections_file_does_not_resolve_airflow_fallback_objects(
    write_sql_connections: Callable[[dict[str, object]], Path],
) -> None:
    write_sql_connections(
        {
            "trino": {
                "type": "trino",
                "host": "trino.example",
                "user": "user",
                "http_scheme": {"from": "extra", "fallback": "https"},
            }
        }
    )

    with pytest.raises(config_module.SqlConfigError, match="http_scheme"):
        config_module.get_connection_config("trino")


def test_transfer_options_allow_two_aliases_with_same_backend() -> None:
    options = api_module.build_transfer_options(
        from_db="gp",
        to_db="gp_sandbox",
        from_sql="select 1",
        to_table="schema.target",
    )

    assert options.from_db_key == "gp"
    assert options.from_db_backend == "gp"
    assert options.to_db_key == "gp_sandbox"
    assert options.to_db_backend == "gp"


def test_transfer_options_enable_clickhouse_host_drop_retry_by_default() -> None:
    options = api_module.build_transfer_options(
        from_db="trino",
        to_db="ch",
        from_sql="select 1",
        to_table="schema.target",
    )
    assert options.ch_retry_per_host_drops is True

    disabled = api_module.build_transfer_options(
        from_db="trino",
        to_db="ch",
        from_sql="select 1",
        to_table="schema.target",
        ch_retry_per_host_drops=False,
    )
    assert disabled.ch_retry_per_host_drops is False

    non_ch = api_module.build_transfer_options(
        from_db="trino",
        to_db="gp",
        from_sql="select 1",
        to_table="schema.target",
    )
    assert non_ch.ch_retry_per_host_drops is False


def test_backend_specific_validation_uses_alias_backend(
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections(
        {
            "target_gp": {
                "type": "gp",
                "host": "gp.example",
                "user": "user",
                "password": "password",
                "database": "db",
            },
            "source_trino": {
                "type": "trino",
                "host": "trino.example",
                "user": "user",
            },
        }
    )

    options = api_module.build_transfer_options(
        from_db="source_trino",
        to_db="target_gp",
        from_sql="select 1",
        to_table="schema.target",
        gp_distributed_by_key=["id"],
    )

    assert options.to_db_key == "target_gp"
    assert options.to_db_backend == "gp"
    assert options.gp_distributed_by_key == ["id"]


def test_transfer_options_accept_scalar_key_columns() -> None:
    options = api_module.build_transfer_options(
        from_db="trino",
        to_db="gp",
        from_sql="select 1",
        to_table="schema.target",
        write_mode="upsert",
        key_columns=" id ",
        gp_distributed_by_key=" id ",
    )

    assert options.key_columns == ["id"]
    assert options.gp_distributed_by_key == ["id"]


def test_transfer_options_reject_non_string_key_columns() -> None:
    with pytest.raises(ValueError, match="gp_distributed_by_key"):
        api_module.build_transfer_options(
            from_db="trino",
            to_db="gp",
            from_sql="select 1",
            to_table="schema.target",
            gp_distributed_by_key=["id", 1],
        )


def test_create_table_sql_accepts_connection_alias() -> None:
    sql = create_sql_table_module.create_sql_table(
        db_key="gp_sandbox",
        table_name="schema.target",
        df=pd.DataFrame({"id": [1], "value": ["x"]}),
        gp_distributed_by_key=["id"],
        only_generate_sql=True,
    )

    assert '"id" BIGINT' in sql
    assert 'DISTRIBUTED BY ("id")' in sql


def test_create_table_sql_accepts_scalar_distribution_key() -> None:
    sql = create_sql_table_module.create_sql_table(
        db_key="gp",
        table_name="schema.target",
        df=pd.DataFrame({"description": ["x"], "value": [1]}),
        gp_distributed_by_key=" description ",
        only_generate_sql=True,
    )

    assert 'DISTRIBUTED BY ("description")' in sql


def test_create_table_from_sql_dry_run_accepts_scalar_distribution_key() -> None:
    plan = create_sql_table_module.create_sql_table(
        db_key="gp",
        table_name="schema.target",
        sql="select description from source_table",
        gp_distributed_by_key="description",
        return_sql=True,
    )

    assert plan.operation == "create_table_from_sql"
    assert plan.options["gp_distributed_by_key"] == ["description"]


def test_create_table_sql_accepts_table_schema_override() -> None:
    gp_sql = create_sql_table_module.create_sql_table(
        db_key="gp",
        table_name="schema.target",
        table_schema={"id": "TEXT", "amount": "NUMERIC(10, 2)"},
        only_generate_sql=True,
    )
    trino_sql = create_sql_table_module.create_sql_table(
        db_key="trino",
        table_name="schema.target",
        table_schema={"id": "VARCHAR", "amount": "DECIMAL(10, 2)"},
        only_generate_sql=True,
    )
    ch_sqls = create_sql_table_module._build_create_table_sqls(
        backend="ch",
        table_name="schema.target",
        df=pd.DataFrame(columns=["id", "amount"]),
        table_schema={"id": "String", "amount": "Decimal(10, 2)"},
        ch_distributed_table=True,
    )

    assert '"id" TEXT' in gp_sql
    assert '"amount" NUMERIC(10, 2)' in gp_sql
    assert '"id" VARCHAR' in trino_sql
    assert '"amount" DECIMAL(10, 2)' in trino_sql
    assert any("`id` String" in sql for sql in ch_sqls)
    assert any("`amount` Decimal(10, 2)" in sql for sql in ch_sqls)


def test_trino_create_table_sql_accepts_partition_and_order_properties() -> None:
    sql = create_sql_table_module.create_sql_table(
        db_key="trino",
        table_name="schema.target",
        df=pd.DataFrame({"dt": ["2026-05-01"], "id": [1]}),
        partition_by=["dt"],
        order_by=["dt", "id"],
        only_generate_sql=True,
    )

    assert "format = 'PARQUET'" in sql
    assert "object_store_layout_enabled = true" in sql
    assert "partitioning = ARRAY['dt']" in sql
    assert "sorted_by = ARRAY['dt', 'id']" in sql


def test_gp_create_table_sql_accepts_initial_partitions_and_rejects_order() -> None:
    sql = create_sql_table_module.create_sql_table(
        db_key="gp",
        table_name="schema.target",
        df=pd.DataFrame({"dt": ["2026-05-01"], "id": [1]}),
        gp_distributed_by_key=["id"],
        partition_by="dt",
        gp_partitions={
            "start": "2026-05-01",
            "end": "2026-07-01",
            "interval": "1 month",
        },
        only_generate_sql=True,
    )

    assert 'DISTRIBUTED BY ("id")' in sql
    assert 'PARTITION BY RANGE ("dt")' in sql
    assert "EVERY (INTERVAL '1 month')" in sql

    with pytest.raises(ValueError, match="order_by is not supported"):
        create_sql_table_module.create_sql_table(
            db_key="gp",
            table_name="schema.target",
            df=pd.DataFrame({"dt": ["2026-05-01"], "id": [1]}),
            order_by=["id"],
            only_generate_sql=True,
        )


@pytest.mark.parametrize(
    ("table_schema", "match"),
    [
        ({"id": "BIGINT", "amount": " "}, "must not be empty"),
    ],
)
def test_create_table_sql_validates_table_schema(
    table_schema: dict[str, str],
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        create_sql_table_module.create_sql_table(
            db_key="gp",
            table_name="schema.target",
            table_schema=table_schema,
            only_generate_sql=True,
        )


def test_create_table_sql_rejects_invalid_table_schema_type() -> None:
    with pytest.raises(TypeError, match="table_schema"):
        create_sql_table_module.create_sql_table(
            db_key="gp",
            table_name="schema.target",
            table_schema=[("id", "BIGINT")],
            only_generate_sql=True,
        )


def test_create_table_sql_rejects_multiple_schema_sources() -> None:
    with pytest.raises(InvalidSqlInputError, match="Exactly one schema source"):
        create_sql_table_module.create_sql_table(
            db_key="gp",
            table_name="schema.target",
            df=pd.DataFrame({"id": [1]}),
            table_schema={"id": "TEXT"},
            only_generate_sql=True,
        )


def test_create_table_from_sql_only_generate_inspects_and_maps_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []

    class FakeConnection:
        def close(self) -> None:
            events.append("close")

    class FakeTargetAdapter:
        def normalize_ch_columns_or_expression(
            self,
            value: object,
            option_name: str,
        ) -> object:
            events.append(("normalize_columns", option_name, value))
            return value

        def normalize_ch_string(self, value: str, option_name: str) -> str:
            events.append(("normalize_string", option_name, value))
            return value

        def validate_gp_distributed_by_key_option(
            self,
            value: object,
            *,
            option_owner: str,
        ) -> None:
            events.append(("validate_gp", value, option_owner))

        normalize_gp_partitions_option = staticmethod(lambda value, **kwargs: value)

        def validate_ch_create_table_options(self, **kwargs: object) -> None:
            events.append(("validate_ch_options", kwargs))

        def validate_ch_columns_in_columns(
            self,
            value: object,
            columns: list[str],
            option_name: str,
            *,
            data_name: str,
        ) -> None:
            events.append(("validate_ch_columns", value, columns, option_name, data_name))

        def build_create_from_sql_target_create_kwargs(
            self,
            **kwargs: object,
        ) -> dict[str, object]:
            events.append(("create_kwargs", kwargs))
            assert kwargs["drop_target_if_exists"] is True
            return {"ch_distributed_table": False}

        build_drop_target_sqls = staticmethod(_fake_drop_target_sqls)

    def fake_config(key: str) -> types.SimpleNamespace:
        backend = "trino" if key == "source_alias" else "gp"
        return types.SimpleNamespace(connection_key=key, backend=backend)

    def fake_inspect(
        backend: str,
        connection: object,
        query: str,
    ) -> list[types.SimpleNamespace]:
        events.append(("inspect", backend, connection, query))
        return [
            types.SimpleNamespace(name="id"),
            types.SimpleNamespace(name="amount"),
        ]

    monkeypatch.setattr(create_sql_table_module, "get_connection_config", fake_config)
    monkeypatch.setattr(
        create_sql_table_module,
        "get_backend_adapter",
        lambda backend: FakeTargetAdapter(),
    )
    monkeypatch.setattr(
        create_sql_table_module,
        "get_sql_connection",
        lambda key: FakeConnection(),
    )
    monkeypatch.setattr(
        transfer_schema_module,
        "inspect_source_query_schema",
        fake_inspect,
    )
    monkeypatch.setattr(
        transfer_schema_module,
        "map_source_schema_to_target",
        lambda source_schema, backend, **_kwargs: {
            column.name: "BIGINT" for column in source_schema
        },
    )
    monkeypatch.setattr(
        operation_runner_module,
        "run_retrying_operation",
        lambda **kwargs: kwargs["operation"](1),
    )
    monkeypatch.setattr(
        create_sql_table_module,
        "_build_create_table_sqls",
        lambda *args, **kwargs: ["create first;", "create second;"],
    )

    generated = create_sql_table_module.create_sql_table(
        db_key="target_alias",
        source_db="source_alias",
        table_name="mart.target",
        sql="select id, amount from source",
        gp_distributed_by_key="id",
        drop_target_if_exists=True,
        only_generate_sql=True,
        query_label="coverage-ddl",
    )

    assert generated == "drop target;\ncreate first;\ncreate second"
    assert "close" in events
    inspect_event = next(event for event in events if event[0] == "inspect")
    assert inspect_event[1] == "trino"
    assert "coverage-ddl" in inspect_event[3]


@pytest.mark.parametrize(
    "schema_source",
    [
        {"df": pd.DataFrame({"id": [1]})},
        {"table_schema": {"id": "BIGINT"}},
    ],
)
def test_create_table_drop_target_runs_before_every_create_attempt(
    monkeypatch: pytest.MonkeyPatch,
    schema_source: dict[str, object],
) -> None:
    events: list[tuple[str, int, bool]] = []
    monkeypatch.setattr(
        create_sql_table_module,
        "get_connection_config",
        lambda key: types.SimpleNamespace(connection_key=key, backend="gp"),
    )
    monkeypatch.setattr(
        create_sql_table_module,
        "_build_create_sql_table_sqls",
        lambda options, option_owner: ["CREATE TABLE mart.target (id BIGINT)"],
    )
    monkeypatch.setattr(
        create_sql_table_module,
        "build_drop_target_sqls",
        lambda options: ["DROP TABLE IF EXISTS mart.target"],
    )
    monkeypatch.setattr(
        create_sql_table_module,
        "drop_existing_target",
        lambda **kwargs: events.append(
            (
                "drop",
                kwargs["retry_attempt"],
                kwargs["options"].drop_target_if_exists,
            )
        ),
    )
    monkeypatch.setattr(
        create_sql_table_module,
        "_execute_create_sql_table",
        lambda **kwargs: events.append(
            (
                "create",
                kwargs["retry_attempt"],
                kwargs["options"].drop_target_if_exists,
            )
        ),
    )

    def fake_run_connection_operation(**kwargs: object) -> None:
        kwargs["operation"]({"connection": object()}, 1)
        context = kwargs["context_factory"](2)
        assert context.phase == "replace_target"
        assert context.sql_preview == "DROP TABLE IF EXISTS mart.target"
        kwargs["operation"]({"connection": object()}, 2)

    monkeypatch.setattr(
        create_sql_table_module,
        "run_connection_operation",
        fake_run_connection_operation,
    )

    result = create_sql_table_module.create_sql_table(
        "gp_alias",
        "mart.target",
        drop_target_if_exists=True,
        return_metadata=True,
        **schema_source,
    )

    assert events == [
        ("drop", 1, True),
        ("create", 1, True),
        ("drop", 2, True),
        ("create", 2, True),
    ]
    assert result.metadata.statement_count == 2
    assert result.plan.sqls == [
        "DROP TABLE IF EXISTS mart.target",
        "CREATE TABLE mart.target (id BIGINT)",
    ]
    assert [statement.phase for statement in result.plan.statements] == [
        "drop_target",
        "create_table",
    ]
    assert result.plan.options["drop_target_if_exists"] is True


@pytest.mark.parametrize(
    "schema_source",
    [
        {"df": pd.DataFrame({"id": [1]})},
        {"table_schema": {"id": "BIGINT"}},
    ],
)
def test_create_table_only_generate_includes_drop_for_regular_schema_sources(
    schema_source: dict[str, object],
) -> None:
    generated = create_sql_table_module.create_sql_table(
        "gp",
        "schema.target",
        drop_target_if_exists=True,
        only_generate_sql=True,
        **schema_source,
    )

    assert generated.startswith("DROP TABLE IF EXISTS schema.target;\nCREATE TABLE")


def test_create_table_target_replace_helper_uses_backend_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[object, ...]] = []

    class FakeAdapter:
        def build_drop_target_sqls(
            self,
            table_name: str,
            **kwargs: object,
        ) -> list[str]:
            events.append(("build", table_name, kwargs))
            return ["DROP TARGET"]

        def prepare_existing_target_for_create_from_sql(
            self,
            connection: object,
            table_name: str,
            **kwargs: object,
        ) -> None:
            events.append(("drop", connection, table_name, kwargs))

    monkeypatch.setattr(
        target_replace_module,
        "get_backend_adapter",
        lambda backend: FakeAdapter(),
    )
    base_options = CreateSqlTableOptions(
        connection_key="gp_alias",
        backend="gp",
        table_name="mart.target",
        df=pd.DataFrame({"id": [1]}),
    )
    metadata = SqlOperationMetadata()

    assert target_replace_module.build_drop_target_sqls(base_options) == []
    target_replace_module.drop_existing_target(
        options=base_options,
        connection=object(),
        drop_sqls=[],
        metadata=metadata,
        retry_attempt=1,
    )
    assert events == []

    replace_options = replace(base_options, drop_target_if_exists=True)
    assert target_replace_module.build_drop_target_sqls(replace_options) == ["DROP TARGET"]
    connection = object()
    target_replace_module.drop_existing_target(
        options=replace_options,
        connection=connection,
        drop_sqls=["DROP TARGET"],
        metadata=metadata,
        retry_attempt=2,
    )

    assert events[0][0:2] == ("build", "mart.target")
    assert events[1][0:3] == ("drop", connection, "mart.target")
    assert events[1][3]["drop_target_if_exists"] is True
    assert events[1][3]["connection_key"] == "gp_alias"
    assert metadata.retry_attempts == 2
    assert metadata.operation_status == "success"


def test_create_table_execution_returns_metadata_and_builds_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    monkeypatch.setattr(
        create_sql_table_module,
        "get_connection_config",
        lambda key: types.SimpleNamespace(connection_key=key, backend="gp"),
    )
    monkeypatch.setattr(
        create_sql_table_module,
        "_build_create_sql_table_sqls",
        lambda options, option_owner: ["create table mart.target (id bigint)"],
    )
    monkeypatch.setattr(
        create_sql_table_module,
        "_execute_create_sql_table",
        lambda **kwargs: events.append(("execute", kwargs)),
    )

    def fake_run_connection_operation(**kwargs: object) -> None:
        context = kwargs["context_factory"](2)
        events.append(("context", context))
        kwargs["operation"]({"connection": object()}, 2)

    monkeypatch.setattr(
        create_sql_table_module,
        "run_connection_operation",
        fake_run_connection_operation,
    )

    result = create_sql_table_module.create_sql_table(
        "gp_alias",
        "mart.target",
        pd.DataFrame({"id": [1]}),
        return_metadata=True,
    )

    assert result.metadata.statement_count == 1
    assert result.plan.operation == "create_table"
    assert result.plan.sqls == ["create table mart.target (id bigint)"]
    context = next(event[1] for event in events if event[0] == "context")
    assert context.retry_attempt == 2
    execution = next(event[1] for event in events if event[0] == "execute")
    assert execution["retry_attempt"] == 2


def test_create_table_with_connection_returns_plan_and_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        create_sql_table_module,
        "_build_create_sql_table_sqls",
        lambda options, option_owner: ["create table target (id bigint)"],
    )
    monkeypatch.setattr(
        create_sql_table_module,
        "_execute_create_sql_table",
        lambda **kwargs: None,
    )

    plan = create_sql_table_module._create_sql_table_with_connection(
        "gp",
        object(),
        "target",
        pd.DataFrame({"id": [1]}),
        dry_run=True,
    )
    result = create_sql_table_module._create_sql_table_with_connection(
        "gp",
        object(),
        "target",
        pd.DataFrame({"id": [1]}),
        return_metadata=True,
    )

    assert plan.operation == "create_table"
    assert result.metadata.statement_count == 1


def test_create_table_dataframe_and_name_validation_edges() -> None:
    with pytest.raises(InvalidSqlInputError, match="Exactly one schema source"):
        create_sql_table_module._resolve_create_dataframe_and_schema(
            df=None,
            table_schema=None,
        )
    with pytest.raises(TypeError, match="df must be a pandas DataFrame"):
        create_sql_table_module._resolve_create_dataframe_and_schema(
            df=[],
            table_schema=None,
        )
    with pytest.raises(InvalidSqlInputError, match="Exactly one schema source"):
        create_sql_table_module._resolve_create_dataframe_and_schema(
            df=pd.DataFrame({"id": [1]}),
            table_schema={"id": "BIGINT"},
        )
    with pytest.raises(ValueError, match="table_name must not be empty"):
        create_sql_table_module.create_sql_table(
            "gp",
            " ",
            pd.DataFrame({"id": [1]}),
            only_generate_sql=True,
        )


def test_connection_backend_and_validation_connection_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[str] = []

    class Connection:
        def close(self) -> None:
            closed.append("closed")

    monkeypatch.setattr(
        config_module,
        "load_sql_connections",
        lambda: {"z": {}, "a": {}},
    )
    monkeypatch.setattr(
        config_module,
        "get_connection_config",
        lambda key: types.SimpleNamespace(connection_key=key, backend="gp"),
    )
    monkeypatch.setattr(
        config_module,
        "_open_validation_connection",
        lambda key: Connection(),
    )

    results = config_module.validate_connections(connect=True)

    assert [result.connection_key for result in results] == ["a", "z"]
    assert all(result.connected for result in results)
    assert closed == ["closed", "closed"]
    assert config_module.get_connection_backend("alias") == "gp"


def test_airflow_validation_and_backend_resolution_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_airflow(
        monkeypatch,
        {
            "AirGP": FakeAirflowConnection(
                conn_type="gp",
                host="gp.example",
                login="user",
                password="password",
                schema="db",
            )
        },
    )

    with config_module.use_airflow_connections(connection_backends={"AirGP": "gp"}):
        results = config_module.validate_connections([" AirGP "])
        assert results[0].connection_key == "AirGP"
        assert config_module.resolve_connection_backend("AirGP") == "gp"

    monkeypatch.setattr(config_module, "get_connection_backend", lambda key: "trino")
    assert config_module.resolve_connection_backend("warehouse") == "trino"


def test_open_validation_connection_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    marker = object()
    monkeypatch.setattr(connection_module, "get_sql_connection", lambda key: marker)

    assert config_module._open_validation_connection("gp") is marker


def test_connection_key_and_empty_airflow_listing_validation() -> None:
    with pytest.raises(SqlConfigError, match="Connection key must not be empty"):
        config_module.normalize_connection_key(" ")

    with config_module.use_airflow_connections():  # noqa: SIM117 -- Python 3.8
        with pytest.raises(SqlConfigError, match="cannot list all"):
            config_module.load_sql_connections()


def test_connection_file_parser_rejects_invalid_top_level_shapes(
    tmp_path: Path,
) -> None:
    path = tmp_path / ".connections"
    path.write_text("[]", encoding="utf-8")
    general_module.set_connections_path(path)
    try:
        with pytest.raises(SqlConfigError, match="must contain a JSON object"):
            config_module.load_sql_connections()
    finally:
        general_module.set_connections_path(None)

    with pytest.raises(SqlConfigError, match="field 'source' must be a string"):
        config_module._is_airflow_connections_file(
            {"source": 1, "connections": {}},
            path,
        )
    with pytest.raises(SqlConfigError, match="keys must be strings"):
        config_module._parse_direct_connections_file({1: {}}, path)
    with pytest.raises(SqlConfigError, match="Duplicate SQL connection key"):
        config_module._parse_direct_connections_file(
            {"GP": {}, " gp ": {}},
            path,
        )
    with pytest.raises(SqlConfigError, match="must be a JSON object"):
        config_module._parse_direct_connections_file({"gp": []}, path)


def test_airflow_file_parser_rejects_invalid_entries(tmp_path: Path) -> None:
    path = tmp_path / ".connections"
    with pytest.raises(SqlConfigError, match="keys must be strings"):
        config_module._parse_airflow_connections_file(
            {"connections": {1: {}}},
            path,
        )
    with pytest.raises(SqlConfigError, match="must be a JSON object"):
        config_module._parse_airflow_connections_file(
            {"connections": {"gp": []}},
            path,
        )
    with pytest.raises(SqlConfigError, match="Duplicate SQL connection key"):
        config_module._parse_airflow_connections_file(
            {
                "connections": {
                    "GP": {"type": "gp"},
                    " gp ": {"type": "gp"},
                }
            },
            path,
        )


def test_airflow_source_entry_normalization_and_unknown_key() -> None:
    entry = config_module._AirflowConnectionEntry(
        connection_id="AirGP",
        backend="gp",
        overrides={},
    )
    source = config_module._AirflowConnectionSource(
        connections={"AirGP": entry},
        normalized_connections={"airgp": "AirGP"},
        default_backend=None,
    )

    assert (
        config_module._get_airflow_source_entry(
            source,
            " airGP ",
            allow_dynamic=False,
        )
        is entry
    )
    with pytest.raises(UnsupportedConnectionTypeError, match="Available keys"):
        config_module._get_airflow_source_entry(
            source,
            "missing",
            allow_dynamic=False,
        )


@pytest.mark.parametrize(
    ("connection", "message"),
    [
        (FakeAirflowConnection(extra_dejson=[]), "extra_dejson must be a dict"),
        (FakeAirflowConnection(extra_dejson=_MISSING, extra={"x": 1}), None),
        (FakeAirflowConnection(extra_dejson=_MISSING, extra="{"), "valid JSON"),
        (FakeAirflowConnection(extra_dejson=_MISSING, extra="[]"), "JSON object"),
        (FakeAirflowConnection(extra_dejson=_MISSING, extra=1), "JSON object"),
    ],
)
def test_airflow_extra_shapes(
    connection: FakeAirflowConnection,
    message: str | None,
) -> None:
    if message is None:
        assert config_module._get_airflow_connection_extras(connection, "id") == {"x": 1}
        return
    with pytest.raises(SqlConfigError, match=message):
        config_module._get_airflow_connection_extras(connection, "id")


def test_airflow_backend_id_and_resolver_edges() -> None:
    with pytest.raises(SqlConfigError, match="does not define a backend"):
        config_module._resolve_airflow_connection_backend(
            FakeAirflowConnection(),
            {},
            "id",
        )
    with pytest.raises(SqlConfigError, match="ID must not be empty"):
        config_module._normalize_airflow_connection_id(" ")

    assert config_module._is_airflow_extra_resolver(
        "settings",
        {"from": "extra", "default": 1, "custom": 2},
    )
    assert (
        config_module._resolve_airflow_entry_overrides(
            {"port": {"from": "extra", "key": "missing"}},
            {},
            "id",
        )
        == {}
    )


@pytest.mark.parametrize("value", [True, "bad", 1.5, 0])
def test_optional_positive_integer_rejects_invalid_values(value: object) -> None:
    with pytest.raises(SqlConfigError):
        config_module._optional_int({"port": value}, "gp", "port", 5432)


@pytest.mark.parametrize("value", [True, "bad", 1.5, -1])
def test_optional_non_negative_integer_rejects_invalid_values(value: object) -> None:
    with pytest.raises(SqlConfigError):
        config_module._optional_non_negative_int({"timeout": value}, "gp", "timeout")


def test_optional_config_value_validation_edges() -> None:
    assert config_module._optional_string({"host": None}, "gp", "host", "x") == "x"
    assert config_module._optional_non_negative_int({"timeout": 0}, "gp", "timeout") == 0
    with pytest.raises(SqlConfigError, match="must be a boolean"):
        config_module._optional_bool({"flag": "maybe"}, "gp", "flag", False)
    with pytest.raises(SqlConfigError, match="boolean or string"):
        config_module._optional_bool_or_string_as_string(
            {"verify": 1},
            "gp",
            "verify",
        )
    with pytest.raises(SqlConfigError, match="only string keys"):
        config_module._optional_mapping({"settings": {1: 2}}, "gp", "settings")
    with pytest.raises(SqlConfigError, match="contain only strings"):
        config_module._optional_string_or_string_list(
            {"ca_certs": [1]},
            "gp",
            "ca_certs",
        )
    with pytest.raises(SqlConfigError, match="string or list of strings"):
        config_module._optional_string_or_string_list(
            {"ca_certs": 1},
            "gp",
            "ca_certs",
        )


def test_airflow_raw_json_extra_and_boolean_type_edges() -> None:
    connection = FakeAirflowConnection(
        extra_dejson=_MISSING,
        extra='{"catalog": "iceberg"}',
    )
    assert config_module._get_airflow_connection_extras(connection, "AirTrino") == {
        "catalog": "iceberg"
    }
    with pytest.raises(SqlConfigError, match="must be a boolean"):
        config_module._optional_bool({"flag": 1}, "gp", "flag", False)


def test_load_sql_connections_lists_explicit_airflow_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_airflow(
        monkeypatch,
        {
            "AirGP": FakeAirflowConnection(
                conn_type="postgres",
                host="gp.example",
                port=5432,
                login="user",
                password="password",
                schema="db",
                extra_dejson={},
            )
        },
    )
    with config_module.use_airflow_connections({"AirGP": "gp"}):
        raw = config_module.load_sql_connections()

    assert raw["AirGP"]["type"] == "gp"


def test_clickhouse_host_connection_validates_backend_host_and_dispatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gp_config = config_module.get_connection_config("gp")
    ch_config = config_module.get_connection_config("ch")
    opened: list[object] = []

    class Backend:
        def open_connection(self, config: object, **kwargs: object) -> object:
            opened.append((config, kwargs))
            return "connection"

    monkeypatch.setattr(connection_module, "get_backend", lambda backend: Backend())
    monkeypatch.setattr(
        connection_module,
        "get_connection_config",
        lambda key: gp_config if key == "gp" else ch_config,
    )

    with pytest.raises(UnsupportedConnectionTypeError, match="not a ClickHouse"):
        connection_module.get_ch_connection_for_host("gp", "host")
    with pytest.raises(ValueError, match="host must not be empty"):
        connection_module.get_ch_connection_for_host("ch", " ")

    assert connection_module.get_ch_connection_for_host("ch", " shard-1 ") == "connection"
    opened_config = opened[0][0]
    assert opened_config.host == "shard-1"


def test_clickhouse_airflow_ca_variable_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = replace(
        config_module.get_connection_config("ch"),
        ca_certs=[],
        ca_certs_variable="ch_ca",
    )

    real_import = builtins.__import__

    def reject_airflow_import(
        name: str,
        globals_: object = None,
        locals_: object = None,
        fromlist: object = (),
        level: int = 0,
    ) -> object:
        if name == "airflow.models.variable":
            message = "airflow unavailable"
            raise ImportError(message)
        return real_import(name, globals_, locals_, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", reject_airflow_import)
    with pytest.raises(SqlConfigError, match="Variable support is unavailable"):
        connection_module._resolve_ch_ca_certs(config)

    monkeypatch.setattr(builtins, "__import__", real_import)
    install_fake_airflow(monkeypatch, {}, {"ch_ca": " "})
    with pytest.raises(SqlConfigError, match="must be a non-empty string"):
        connection_module._resolve_ch_ca_certs(config)


def test_certificate_bundle_reuse_and_absolute_path() -> None:
    first = _write_cert(".certs/first.pem", "FIRST\n")
    second = _write_cert(".certs/second.pem", "SECOND\n")

    bundle = connection_module._resolve_ca_certs(
        "alias with spaces",
        [first.name, second.name],
    )
    same_bundle = connection_module._resolve_ca_certs(
        "alias with spaces",
        [first.name, second.name],
    )

    assert bundle == same_bundle
    assert Path(bundle).read_text(encoding="utf-8") == "FIRST\nSECOND\n"
    assert (
        connection_module._resolve_single_cert_path(
            "alias",
            str(first),
            field_name="ca_certs",
        )
        == first.resolve()
    )

    with pytest.raises(InvalidSqlInputError, match="Exactly one schema source"):
        create_sql_table_module.create_sql_table(
            db_key="gp",
            table_name="schema.target",
            sql="select 1 as id",
            table_schema={"id": "BIGINT"},
            only_generate_sql=True,
        )


def test_trino_insert_chunk_size_comes_from_connection_config(
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections(
        {
            "trino_batch": {
                "type": "trino",
                "host": "trino.example",
                "user": "user",
                "insert_chunk_size": 250,
            }
        }
    )

    config = config_module.get_connection_config("trino_batch")

    assert config.insert_chunk_size == 250
    assert load_sql_table_module._get_trino_insert_chunk_size(None, "trino_batch") == 250


def test_legacy_trino_insert_chunk_size_env_is_ignored(monkeypatch) -> None:
    from analytics_toolkit.sql.backends.trino.insert import (
        DEFAULT_TRINO_INSERT_CHUNK_SIZE,
    )

    monkeypatch.setenv("TRINO_INSERT_CHUNK_SIZE", "2")

    assert (
        load_sql_table_module._get_trino_insert_chunk_size(None, "trino")
        == DEFAULT_TRINO_INSERT_CHUNK_SIZE
    )


@pytest.mark.parametrize(
    ("backend", "raw_config"),
    [
        (
            "gp",
            {
                "type": "gp",
                "host": "gp.example",
                "user": "user",
                "password": "password",
                "database": "db",
                "transfer_staging_schema": "transfer_gp",
            },
        ),
        (
            "trino",
            {
                "type": "trino",
                "host": "trino.example",
                "user": "user",
                "password": "password",
                "catalog": "iceberg",
                "schema": "sandbox",
                "transfer_staging_schema": "transfer_trino",
            },
        ),
        (
            "ch",
            {
                "type": "ch",
                "host": "ch.example",
                "user": "user",
                "password": "password",
                "database": "default",
                "transfer_staging_schema": "transfer_ch",
            },
        ),
    ],
)
def test_direct_connections_file_supports_transfer_staging_schema(
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
    backend: str,
    raw_config: dict[str, object],
) -> None:
    alias = f"{backend}_with_staging"
    write_sql_connections({alias: raw_config})

    config = config_module.get_connection_config(alias)
    assert config.transfer_staging_schema == f"transfer_{backend}"


def test_direct_trino_connections_file_supports_s3_transfer_staging_location(
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
) -> None:
    write_sql_connections(
        {
            "trino_with_location": {
                "type": "trino",
                "host": "trino.example",
                "user": "user",
                "password": "password",
                "catalog": "iceberg",
                "schema": "sandbox",
                "transfer_staging_schema": "object_storage.sandbox",
                "s3_transfer_staging_schema": "hive.sandbox",
                "s3_transfer_staging_location": "s3://bucket/tmp/analytics_toolkit_transfer",
            },
        }
    )

    config = config_module.get_connection_config("trino_with_location")
    assert config.transfer_staging_schema == "object_storage.sandbox"
    assert config.s3_transfer_staging_schema == "hive.sandbox"
    assert config.s3_transfer_staging_location == "s3://bucket/tmp/analytics_toolkit_transfer"


def _direct_trino_config(**overrides: object) -> types.SimpleNamespace:
    values: dict[str, object] = {
        "connection_key": "warehouse",
        "host": "trino.example",
        "port": 8443,
        "user": "analyst",
        "password": "secret",
        "catalog": "iceberg",
        "schema": "analytics",
        "auth_mode": "basic",
        "http_scheme": "https",
        "verify_value": "true",
        "ca_certs": [],
        "request_timeout": 17,
        "source": "coverage",
    }
    values.update(overrides)
    return types.SimpleNamespace(**values)


def test_direct_trino_connection_import_auth_and_kwargs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_import = builtins.__import__

    def blocked_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "trino" or name.startswith("trino."):
            message = "blocked"
            raise ImportError(message)
        return real_import(name, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(builtins, "__import__", blocked_import)
        context.delitem(sys.modules, "trino", raising=False)
        context.delitem(sys.modules, "trino.auth", raising=False)
        with pytest.raises(ImportError, match="required for Trino"):
            trino_config_module.open_connection(
                _direct_trino_config(),
                parse_verify_value=lambda value: value,
                resolve_ca_certs=lambda *_args: None,
            )

    calls: list[dict[str, object]] = []
    _install_fake_trino(monkeypatch, calls)
    assert (
        trino_config_module.open_connection(
            _direct_trino_config(),
            parse_verify_value=lambda value: value == "/ca.pem",
            resolve_ca_certs=lambda *_args: "/ca.pem",
        )
        is not None
    )
    kwargs = calls[-1]
    assert kwargs["verify"] is True
    assert kwargs["catalog"] == "iceberg"
    assert kwargs["schema"] == "analytics"
    assert kwargs["request_timeout"] == 17
    assert kwargs["source"] == "coverage"
    assert kwargs["auth"].password == "secret"

    trino_config_module.open_connection(
        _direct_trino_config(
            auth_mode="oauth2",
            password=None,
            catalog=None,
            schema=None,
            request_timeout=None,
            source=None,
        ),
        parse_verify_value=lambda value: value,
        resolve_ca_certs=lambda *_args: None,
    )
    assert calls[-1]["auth"] is not None
    assert "catalog" not in calls[-1]
    assert "source" not in calls[-1]

    trino_config_module.open_connection(
        _direct_trino_config(password=None),
        parse_verify_value=lambda value: value,
        resolve_ca_certs=lambda *_args: None,
    )
    assert calls[-1]["auth"] is None
    with pytest.raises(SqlConfigError, match="unsupported auth_mode"):
        trino_config_module.open_connection(
            _direct_trino_config(auth_mode="kerberos"),
            parse_verify_value=lambda value: value,
            resolve_ca_certs=lambda *_args: None,
        )


@pytest.mark.parametrize(
    ("backend", "raw_config"),
    [
        (
            "gp",
            {
                "type": "gp",
                "host": "gp.example",
                "user": "user",
                "password": "password",
                "database": "db",
            },
        ),
        (
            "trino",
            {
                "type": "trino",
                "host": "trino.example",
                "user": "user",
                "password": "password",
                "catalog": "iceberg",
                "schema": "sandbox",
            },
        ),
        (
            "ch",
            {
                "type": "ch",
                "host": "ch.example",
                "user": "user",
                "password": "password",
                "database": "default",
            },
        ),
    ],
)
def test_direct_connections_without_transfer_staging_schema_keep_default_behavior(
    write_sql_connections: Callable[[dict[str, dict[str, object]]], Path],
    backend: str,
    raw_config: dict[str, object],
) -> None:
    alias = f"{backend}_without_staging"
    write_sql_connections({alias: raw_config})

    config = config_module.get_connection_config(alias)
    assert config.transfer_staging_schema is None


def test_airflow_connections_file_supports_transfer_staging_schema(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, object]], Path],
) -> None:
    write_sql_connections(
        {
            "source": "airflow",
            "connections": {
                "airflow_gp": {"type": "gp", "transfer_staging_schema": "airflow_transfer_gp"},
                "airflow_trino": {
                    "type": "trino",
                    "transfer_staging_schema": "airflow_transfer_trino",
                },
                "airflow_ch": {"type": "ch", "transfer_staging_schema": "airflow_transfer_ch"},
            },
        }
    )
    install_fake_airflow(
        monkeypatch,
        {
            "airflow_gp": FakeAirflowConnection(
                conn_type="postgres",
                host="air-gp.example",
                login="air-user",
                password="air-password",
                schema="air_db",
            ),
            "airflow_trino": FakeAirflowConnection(
                conn_type="trino",
                host="air-trino.example",
                login="air-user",
                password="air-password",
            ),
            "airflow_ch": FakeAirflowConnection(
                conn_type="clickhouse",
                host="air-ch.example",
                login="ch-user",
                password="ch-password",
                schema="default",
            ),
        },
    )

    assert config_module.get_connection_config("airflow_gp").transfer_staging_schema == (
        "airflow_transfer_gp"
    )
    assert config_module.get_connection_config("airflow_trino").transfer_staging_schema == (
        "airflow_transfer_trino"
    )
    assert config_module.get_connection_config("airflow_ch").transfer_staging_schema == (
        "airflow_transfer_ch"
    )


def test_airflow_trino_connections_file_supports_s3_transfer_staging_location(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections: Callable[[dict[str, object]], Path],
) -> None:
    write_sql_connections(
        {
            "source": "airflow",
            "connections": {
                "airflow_trino": {
                        "type": "trino",
                        "transfer_staging_schema": "object_storage.sandbox",
                        "s3_transfer_staging_schema": "hive.sandbox",
                    "s3_transfer_staging_location": {
                        "from": "extra",
                        "key": "parquet_transfer_location",
                    },
                },
            },
        }
    )
    install_fake_airflow(
        monkeypatch,
        {
            "airflow_trino": FakeAirflowConnection(
                conn_type="trino",
                host="air-trino.example",
                login="air-user",
                password="air-password",
                extra_dejson={
                    "parquet_transfer_location": ("s3://bucket/tmp/analytics_toolkit_transfer"),
                },
            ),
        },
    )

    config = config_module.get_connection_config("airflow_trino")
    assert config.transfer_staging_schema == "object_storage.sandbox"
    assert config.s3_transfer_staging_schema == "hive.sandbox"
    assert config.s3_transfer_staging_location == "s3://bucket/tmp/analytics_toolkit_transfer"


def test_direct_clickhouse_connection_reports_blocked_connector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_import = builtins.__import__

    def blocked_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "clickhouse_connect" or name.startswith("clickhouse_connect."):
            message = "blocked"
            raise ImportError(message)
        return real_import(name, *args, **kwargs)

    config = types.SimpleNamespace(
        host="clickhouse.example",
        port=8443,
        user="analyst",
        password="secret",
        secure=True,
        database=None,
        verify_value=None,
        connect_timeout=None,
        send_receive_timeout=None,
        settings=None,
        interface=None,
        query_limit=None,
        query_retries=None,
        client_name=None,
    )
    with monkeypatch.context() as context:
        context.setattr(builtins, "__import__", blocked_import)
        context.delitem(sys.modules, "clickhouse_connect", raising=False)
        context.delitem(sys.modules, "clickhouse_connect.common", raising=False)
        with pytest.raises(ImportError, match="required for ClickHouse"):
            ch_config_module.open_connection(
                config,
                parse_verify_value=lambda value: value,
                resolve_ch_ca_certs=lambda _config: None,
            )
