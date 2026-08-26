from __future__ import annotations

from tests.sql._support.connection_config import (
    Callable,
    InvalidSqlInputError,
    Path,
    _install_fake_trino,
    _write_cert,
    config_module,
    connection_module,
    create_sql_table_module,
    pytest,
)


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
