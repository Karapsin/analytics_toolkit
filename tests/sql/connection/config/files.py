from __future__ import annotations

from tests.sql._support.connection_config import (
    Path,
    SqlConfigError,
    _resolve_calling_base_dir,
    config_module,
    config_path_module,
    connection_module,
    connections_state_module,
    general_module,
    json,
    pytest,
    sys,
    types,
)


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


def test_connections_file_lookup_prefers_calling_script_to_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script_root = tmp_path / "airflow_project"
    script_dir = script_root / "dags" / "tasks"
    script_dir.mkdir(parents=True)
    script_connections = script_root / ".connections"
    script_connections.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config_path_module, "_resolve_calling_base_dir", lambda: script_dir)

    general_module.set_connections_path(None)

    assert config_module.get_connections_file_path() == script_connections.resolve()


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
    assert connections_state_module.get_last_connections_path() == recovered_connections.resolve()


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


def test_connections_file_read_does_not_retry_other_os_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connections_path = tmp_path / ".connections"

    def fail_rediscovery() -> Path | None:
        message = "unexpected recovery lookup"
        raise AssertionError(message)

    def raise_permission_error(
        _path: Path,
        encoding: str | None = None,
        errors: str | None = None,
    ) -> str:
        del encoding, errors
        message = "permission denied"
        raise PermissionError(message)

    monkeypatch.setattr(config_module, "get_connections_file_path", lambda: connections_path)
    monkeypatch.setattr(config_module, "find_connections_file_path", fail_rediscovery)
    monkeypatch.setattr(Path, "read_text", raise_permission_error)

    with pytest.raises(PermissionError, match="permission denied"):
        config_module._read_connections_file_text()


def test_connections_file_read_race_stops_after_five_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connections_path = tmp_path / ".connections"
    read_attempts = 0
    recovery_attempts = 0

    def missing_read_text(
        _path: Path,
        encoding: str | None = None,
        errors: str | None = None,
    ) -> str:
        nonlocal read_attempts
        del encoding, errors
        read_attempts += 1
        raise FileNotFoundError(2, "No such file", str(connections_path))

    def rediscover() -> Path:
        nonlocal recovery_attempts
        recovery_attempts += 1
        return connections_path

    monkeypatch.setattr(config_module, "get_connections_file_path", lambda: connections_path)
    monkeypatch.setattr(config_module, "find_connections_file_path", rediscover)
    monkeypatch.setattr(Path, "read_text", missing_read_text)

    with pytest.raises(
        SqlConfigError,
        match="disappeared while being read after 5 recovery retries",
    ) as exc_info:
        config_module._read_connections_file_text()

    assert isinstance(exc_info.value.__cause__, FileNotFoundError)
    assert read_attempts == 6
    assert recovery_attempts == 5


def test_connections_file_read_recovers_from_rotation_race(
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
    script_connections.write_text(
        json.dumps(
            {
                "gp": {
                    "type": "gp",
                    "host": "current-gp.example",
                    "user": "user",
                    "password": "password",
                    "database": "db",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_path_module, "_resolve_calling_base_dir", lambda: script_dir)
    original_read_text = Path.read_text
    read_paths: list[Path] = []

    def read_text_with_rotation(
        path: Path,
        encoding: str | None = None,
        errors: str | None = None,
    ) -> str:
        read_paths.append(path)
        if path == old_connections.resolve():
            path.unlink()
            raise FileNotFoundError(2, "No such file", str(path))
        return original_read_text(path, encoding=encoding, errors=errors)

    general_module.set_connections_path(old_connections)
    monkeypatch.setattr(Path, "read_text", read_text_with_rotation)

    config = config_module.get_connection_config("gp")

    assert config.host == "current-gp.example"
    assert read_paths == [old_connections.resolve(), script_connections.resolve()]
    assert connections_state_module.get_connections_path_override() == script_connections.resolve()


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
        config_path_module._iter_search_directories([nested_dir, nested_dir.parent])
    )

    assert search_directories.count(nested_dir.parent.resolve()) == 1


def test_malformed_connections_file_raises_config_error(tmp_path: Path) -> None:
    (tmp_path / ".connections").write_text("{not json", encoding="utf-8")

    with pytest.raises(config_module.SqlConfigError):
        config_module.get_connection_config("gp")


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
        types.SimpleNamespace(connect=lambda **kwargs: connect_calls.append(kwargs) or object()),
    )
    monkeypatch.setattr(config_path_module, "_resolve_calling_base_dir", lambda: None)
    original_read_text = Path.read_text

    def read_text_with_rotation(
        path: Path,
        encoding: str | None = None,
        errors: str | None = None,
    ) -> str:
        if path == old_connections.resolve():
            path.unlink()
            raise FileNotFoundError(2, "No such file", str(path))
        return original_read_text(path, encoding=encoding, errors=errors)

    general_module.set_connections_path(old_connections)
    monkeypatch.setattr(Path, "read_text", read_text_with_rotation)
    connection_module.get_sql_connection("gp_ssl")

    assert connect_calls[0]["sslrootcert"] == str(ca_path.resolve())
    assert (
        connections_state_module.get_connections_path_override() == recovered_connections.resolve()
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
    monkeypatch.setattr(config_path_module, "_resolve_calling_base_dir", lambda: caller_dir)

    general_module.set_connections_path(old_connections)
    old_connections.unlink()

    with pytest.raises(SqlConfigError, match="must contain valid JSON"):
        config_module.get_connection_config("gp")

    assert (
        connections_state_module.get_connections_path_override() == recovered_connections.resolve()
    )
