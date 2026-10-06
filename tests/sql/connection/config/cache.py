from __future__ import annotations

import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from typing import Any

from analytics_toolkit import sql

from tests._support.paths import REPO_ROOT
from tests.conftest import DEFAULT_SQL_CONNECTIONS
from tests.sql._support.connection_config import (
    FakeAirflowConnection,
    config_module,
    connection_module,
    general_module,
    install_fake_airflow,
    json,
    pytest,
    sys,
    types,
)


def test_helpers_share_snapshot_and_keep_opening_fresh_connections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / ".connections"
    reads: list[Path] = []
    parses: list[str] = []
    connections: list[dict[str, Any]] = []
    original_read = Path.read_text
    original_parse = json.loads

    def read(file: Path, *args: Any, **kwargs: Any) -> str:
        if file == path:
            reads.append(file)
        return original_read(file, *args, **kwargs)

    def parse(text: str, *args: Any, **kwargs: Any) -> Any:
        parses.append(text)
        return original_parse(text, *args, **kwargs)

    def connect(**kwargs: Any) -> object:
        connections.append(kwargs)
        return types.SimpleNamespace(close=lambda: None)

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(json, "loads", parse)
    monkeypatch.setitem(sys.modules, "psycopg2", types.SimpleNamespace(connect=connect))

    assert config_module.get_connection_config("gp").host == "gp.example"
    assert config_module.get_connection_backend("trino") == "trino"
    assert set(config_module.load_sql_connections()) == set(DEFAULT_SQL_CONNECTIONS)
    sql.execute("gp", "select 1", dry_run=True)
    connection_module.get_sql_connection("gp")
    connection_module.get_sql_connection("gp")

    assert reads == [path]
    assert len(parses) == 1
    assert len(connections) == 2


def test_cached_file_survives_edits_deletion_and_directory_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / ".connections"
    assert config_module.get_connection_config("gp").host == "gp.example"
    path.write_text("{invalid edited JSON", encoding="utf-8")
    assert config_module.get_connection_config("gp").host == "gp.example"
    path.unlink()
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    (other_dir / ".connections").write_text("{}", encoding="utf-8")
    monkeypatch.chdir(other_dir)

    assert config_module.get_connection_config("gp").host == "gp.example"
    assert config_module.get_connections_file_path() == path
    assert config_module.load_sql_connections()["gp"]["host"] == "gp.example"


def test_explicit_selection_replaces_snapshot_and_same_path_reloads(
    tmp_path: Path,
) -> None:
    path_a = tmp_path / ".connections"
    config_module.get_connection_config("gp")
    updated = deepcopy(DEFAULT_SQL_CONNECTIONS)
    updated["gp"]["host"] = "updated-a.example"
    path_a.write_text(json.dumps(updated), encoding="utf-8")
    directory_b = tmp_path / "b"
    directory_b.mkdir()
    path_b = directory_b / ".connections"
    updated["gp"]["host"] = "b.example"
    path_b.write_text(json.dumps(updated), encoding="utf-8")

    general_module.set_connections_path(path_b)
    assert config_module.get_connection_config("gp").host == "b.example"
    general_module.set_connections_path(path_a)
    assert config_module.get_connection_config("gp").host == "updated-a.example"
    updated["gp"]["host"] = "reloaded-a.example"
    path_a.write_text(json.dumps(updated), encoding="utf-8")
    general_module.set_connections_path(path_a)
    assert config_module.get_connection_config("gp").host == "reloaded-a.example"
    general_module.set_connections_path(path_b)
    assert config_module.get_connection_config("gp").host == "b.example"
    general_module.set_connections_path(None)
    assert config_module.get_connection_config("gp").host == "reloaded-a.example"


def test_invalid_selection_preserves_snapshot(tmp_path: Path) -> None:
    path = tmp_path / ".connections"
    config_module.get_connection_config("gp")
    path.unlink()

    for invalid in (tmp_path / "wrong-name", tmp_path / "missing" / ".connections"):
        with pytest.raises(ValueError):
            general_module.set_connections_path(invalid)
        assert config_module.get_connection_config("gp").host == "gp.example"


def test_failed_parse_is_not_cached_but_missing_key_keeps_successful_snapshot(
    tmp_path: Path,
) -> None:
    path = tmp_path / ".connections"
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(config_module.SqlConfigError, match="valid JSON"):
        config_module.get_connection_config("gp")
    path.write_text(json.dumps(DEFAULT_SQL_CONNECTIONS), encoding="utf-8")
    with pytest.raises(config_module.UnsupportedConnectionTypeError, match="new"):
        config_module.get_connection_config("new")
    updated = deepcopy(DEFAULT_SQL_CONNECTIONS)
    updated["new"] = dict(updated["gp"])
    path.write_text(json.dumps(updated), encoding="utf-8")
    with pytest.raises(config_module.UnsupportedConnectionTypeError, match="new"):
        config_module.get_connection_config("new")
    general_module.set_connections_path(path)
    assert config_module.get_connection_config("new").host == "gp.example"


def test_nested_return_values_cannot_mutate_snapshot(tmp_path: Path) -> None:
    raw = config_module.load_sql_connections()
    raw["gp"]["host"] = "mutated.example"
    raw["ch"]["ddl_defaults"]["regular"]["shard"]["engine"] = "Memory"

    assert config_module.get_connection_config("gp").host == "gp.example"
    assert (
        config_module.load_sql_connections()["ch"]["ddl_defaults"]["regular"]["shard"]["engine"]
        == "ReplicatedMergeTree"
    )


def test_concurrent_helpers_load_one_consistent_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / ".connections"
    reads: list[Path] = []
    original_read = Path.read_text

    def read(file: Path, *args: Any, **kwargs: Any) -> str:
        if file == path:
            reads.append(file)
        return original_read(file, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    with ThreadPoolExecutor(max_workers=8) as executor:
        configs = list(executor.map(config_module.get_connection_config, ["gp"] * 16))

    assert [config.host for config in configs] == ["gp.example"] * 16
    assert reads == [path]


def test_references_and_sibling_paths_stay_fresh_after_connections_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {"DATABASE": "first_db"}
    install_fake_airflow(monkeypatch, {}, values)
    monkeypatch.setenv("HOST", "first.example")
    path = tmp_path / ".connections"
    config = dict(DEFAULT_SQL_CONNECTIONS["gp"])
    config.update(
        host={"from": "env", "key": "HOST"},
        password={"from": ".secrets", "key": "PASSWORD"},
        database={"from": "airflow_variable", "key": "DATABASE"},
        ca_certs="root.pem",
    )
    path.write_text(json.dumps({"gp": config}), encoding="utf-8")
    secrets = tmp_path / ".secrets"
    secrets.write_text("export PASSWORD='first'\n", encoding="utf-8")
    secrets.chmod(0o600)
    cert = tmp_path / ".certs" / "root.pem"
    cert.parent.mkdir()
    cert.write_text("CERT", encoding="utf-8")
    first = config_module.get_connection_config("gp")
    assert (first.host, first.password, first.database) == ("first.example", "first", "first_db")
    path.unlink()
    monkeypatch.setenv("HOST", "second.example")
    values["DATABASE"] = "second_db"
    secrets.write_text("export PASSWORD='second'\n", encoding="utf-8")
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)

    second = config_module.get_connection_config("gp")
    assert (second.host, second.password, second.database) == (
        "second.example",
        "second",
        "second_db",
    )
    assert (
        connection_module._resolve_single_cert_path("gp", "root.pem", field_name="ca_certs") == cert
    )


def test_file_airflow_entries_are_cached_but_credentials_are_refetched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    airflow_connection = FakeAirflowConnection(
        conn_type="postgres", host="first.example", login="user", password="first", schema="db"
    )
    install_fake_airflow(monkeypatch, {"warehouse": airflow_connection})
    path = tmp_path / ".connections"
    path.write_text(
        json.dumps(
            {
                "source": "airflow",
                "connections": {"gp": {"type": "gp", "connection_id": "warehouse"}},
            }
        ),
        encoding="utf-8",
    )

    assert config_module.get_connection_config("gp").password == "first"
    path.unlink()
    airflow_connection.password = "second"
    airflow_connection.host = "second.example"
    second = config_module.get_connection_config("gp")
    assert (second.host, second.password) == ("second.example", "second")
    assert config_module.load_sql_connections()["gp"]["password"] == "second"


def test_new_interpreter_reads_updated_entries(tmp_path: Path) -> None:
    path = tmp_path / ".connections"
    assert config_module.get_connection_config("gp").host == "gp.example"
    updated = deepcopy(DEFAULT_SQL_CONNECTIONS)
    updated["gp"]["host"] = "fresh-process.example"
    path.write_text(json.dumps(updated), encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    script = (
        "import sys\n"
        "from analytics_toolkit import general\n"
        "from analytics_toolkit.sql.connection.config import get_connection_config\n"
        "general.set_connections_path(sys.argv[1])\n"
        "assert get_connection_config('gp').host == 'fresh-process.example'\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script, str(path)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert config_module.get_connection_config("gp").host == "gp.example"
