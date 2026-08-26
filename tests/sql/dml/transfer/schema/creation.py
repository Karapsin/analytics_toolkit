from __future__ import annotations

from tests.sql._support.transfer_schema import (
    gp_adapter_module,
    pd,
    pytest,
    stage_module,
)


def test_build_stage_table_name_keeps_gp_identifier_within_limit() -> None:
    stage_name = stage_module.build_stage_table_name(
        "gp",
        "sales.karapsin_temp_users_po",
        transfer_staging_schema="transfer_schema",
        transfer_staging_username="karapsin_de",
        random_suffix="4f99601c",
    )

    stage_identifier = stage_name.split(".")[-1]
    assert len(stage_identifier.encode()) <= gp_adapter_module.GP_IDENTIFIER_MAX_BYTES
    assert stage_identifier.endswith("__stage__4f99601c")
    assert stage_identifier.startswith("karap")
    assert not stage_identifier.startswith("karapsin_temp_users_po__")


def test_build_stage_table_name_keeps_gp_identifier_within_limit_without_username() -> None:
    stage_name = stage_module.build_stage_table_name(
        "gp",
        "sales.very_long_target_table_name_for_monthly_analytics_exports",
        transfer_staging_schema="transfer_schema",
        random_suffix="4f99601c",
    )

    stage_identifier = stage_name.split(".")[-1]
    assert len(stage_identifier.encode()) <= gp_adapter_module.GP_IDENTIFIER_MAX_BYTES
    assert stage_identifier.endswith("__stage__4f99601c")
    assert "__analytics_toolkit_" not in stage_identifier
    assert not stage_identifier.startswith(
        "very_long_target_table_name_for_monthly_analytics_exports__"
    )


def test_build_stage_table_name_keeps_legacy_naming_without_transfer_schema() -> None:
    stage_name = stage_module.build_stage_table_name(
        "gp",
        "sales.target",
        random_suffix="abcd",
    )

    assert stage_name == "sales.target__stage__abcd"


def test_build_stage_table_name_uses_transfer_staging_schema_and_username() -> None:
    stage_name = stage_module.build_stage_table_name(
        "gp",
        "sales.target",
        transfer_staging_schema="transfer_schema",
        transfer_staging_username="loader",
        random_suffix="abcd",
    )

    assert stage_name == "transfer_schema.target__analytics_toolkit_loader__stage__abcd"


@pytest.mark.parametrize(
    ("username", "expected"),
    [
        (None, "target__stage__"),
        ("loader", "target__analytics_toolkit_loader__stage__"),
    ],
)
def test_build_stage_table_prefix(username: str | None, expected: str) -> None:
    assert stage_module.build_stage_table_prefix("gp", "sales.target", username) == expected


def test_create_stage_table_exhausts_generated_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stage_module, "STAGE_TABLE_NAME_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(stage_module, "table_exists", lambda *args, **kwargs: True)
    monkeypatch.setattr(stage_module, "time_print", lambda message: None)

    with pytest.raises(RuntimeError, match="unique stage table name after 2 attempts"):
        stage_module.create_stage_table(
            "trino",
            object(),
            "sales.target",
            pd.DataFrame({"id": [1]}),
        )


def test_create_stage_table_fixed_suffix_collision_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        stage_module,
        "table_exists",
        lambda *args, **kwargs: True,
    )

    with pytest.raises(RuntimeError, match="Stage table name collision detected"):
        stage_module.create_stage_table(
            "gp",
            object(),
            "sales.target",
            pd.DataFrame({"id": [1]}),
            random_suffix="fixed",
        )


def test_create_stage_table_uses_batch_when_schema_is_not_supplied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch = pd.DataFrame({"id": [1]})
    captured: dict[str, object] = {}
    monkeypatch.setattr(stage_module, "table_exists", lambda *args, **kwargs: False)

    def fake_create(*args: object, **kwargs: object) -> None:
        captured["args"] = args
        captured["kwargs"] = kwargs

    monkeypatch.setattr(stage_module, "_create_sql_table_with_connection", fake_create)

    result = stage_module.create_stage_table(
        "ch",
        object(),
        "analytics.target",
        batch,
        random_suffix="fixed",
    )

    assert result == "analytics.target__stage__fixed"
    assert captured["args"][3] is batch
    assert captured["kwargs"] == {
        "connection_key": "ch",
        "ddl_scope": "staging",
        "gp_distributed_by_key": None,
    }
