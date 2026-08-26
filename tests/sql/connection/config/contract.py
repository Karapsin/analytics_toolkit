from __future__ import annotations

from tests._support.paths import REPO_ROOT
from tests.sql._support.connection_config import (
    Callable,
    FakeAirflowConnection,
    Path,
    config_module,
    connection_module,
    connections_state_module,
    general_module,
    install_fake_airflow,
    json,
    os,
    pytest,
    subprocess,
    sys,
    types,
)


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
    repo_root = REPO_ROOT
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
