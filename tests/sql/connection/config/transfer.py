from __future__ import annotations

from tests.sql._support.connection_config import (
    FakeAirflowConnection,
    api_module,
    config_module,
    install_fake_airflow,
    pytest,
)


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


def test_transfer_options_reject_non_string_key_columns() -> None:
    with pytest.raises(ValueError, match="gp_distributed_by_key"):
        api_module.build_transfer_options(
            from_db="trino",
            to_db="gp",
            from_sql="select 1",
            to_table="schema.target",
            gp_distributed_by_key=["id", 1],
        )


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
