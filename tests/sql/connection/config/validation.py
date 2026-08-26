from __future__ import annotations

from tests.sql._support.connection_config import (
    Callable,
    Path,
    SqlConfigError,
    api_module,
    builtins,
    config_module,
    config_path_module,
    connection_module,
    connections_state_module,
    general_module,
    pytest,
    sys,
    types,
)


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


def test_connection_key_and_empty_airflow_listing_validation() -> None:
    with pytest.raises(SqlConfigError, match="Connection key must not be empty"):
        config_module.normalize_connection_key(" ")

    with config_module.use_airflow_connections():  # noqa: SIM117 -- Python 3.8
        with pytest.raises(SqlConfigError, match="cannot list all"):
            config_module.load_sql_connections()


def test_generate_dummy_connections_rejects_existing_file(tmp_path: Path) -> None:
    connections_path = tmp_path / ".connections"
    original_content = connections_path.read_text(encoding="utf-8")

    with pytest.raises(ValueError, match="SQL connections file already exists"):
        config_module.generate_dummy_connections()

    assert connections_path.read_text(encoding="utf-8") == original_content


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
    monkeypatch.setattr(config_path_module, "_resolve_calling_base_dir", lambda: script_dir)
    monkeypatch.chdir(cwd_dir)

    general_module.set_connections_path(old_connections)
    old_connections.unlink()

    assert config_module.get_connections_file_path() == script_connections.resolve()
    assert connections_state_module.get_connections_path_override() == script_connections.resolve()


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


def test_open_validation_connection_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    marker = object()
    monkeypatch.setattr(connection_module, "get_sql_connection", lambda key: marker)

    assert config_module._open_validation_connection("gp") is marker


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


@pytest.mark.parametrize("value", [True, "bad", 1.5, -1])
def test_optional_non_negative_integer_rejects_invalid_values(value: object) -> None:
    with pytest.raises(SqlConfigError):
        config_module._optional_non_negative_int({"timeout": value}, "gp", "timeout")


@pytest.mark.parametrize("value", [True, "bad", 1.5, 0])
def test_optional_positive_integer_rejects_invalid_values(value: object) -> None:
    with pytest.raises(SqlConfigError):
        config_module._optional_int({"port": value}, "gp", "port", 5432)


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


def test_unknown_connection_key_raises_config_error() -> None:
    with pytest.raises(config_module.UnsupportedConnectionTypeError):
        config_module.get_connection_config("missing")
