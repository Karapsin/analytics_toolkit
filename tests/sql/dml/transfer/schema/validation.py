from __future__ import annotations

from tests.sql._support.transfer_schema import (
    FakeClickHouseClient,
    pytest,
    schema_module,
    stage_module,
)


def test_existing_target_insert_types_reject_missing_columns() -> None:
    client = FakeClickHouseClient()

    try:
        schema_module.get_existing_target_insert_types(
            "ch",
            client,
            "target",
            {
                "id": "Nullable(Int32)",
                "missing": "Nullable(String)",
            },
            connection_key="ch",
        )
    except ValueError as exc:
        assert "missing" in str(exc)
    else:
        raise AssertionError("Expected missing target column to raise.")


def test_map_source_schema_to_target_falls_back_for_invalid_decimal_bounds() -> None:
    source_schema = [
        schema_module.SourceColumn("quantity", "numeric(65535, 0)"),
    ]

    assert schema_module.map_source_schema_to_target(source_schema, "gp") == {
        "quantity": "NUMERIC",
    }
    assert schema_module.map_source_schema_to_target(source_schema, "trino") == {
        "quantity": "DECIMAL(38, 10)",
    }
    assert schema_module.map_source_schema_to_target(source_schema, "ch") == {
        "quantity": "Nullable(Decimal(38, 10))",
    }


def test_stage_name_parser_guards_reject_injected_invalid_nodes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stage_module, "parse_one", lambda *_args, **_kwargs: object())
    with pytest.raises(ValueError, match="Invalid target table"):
        stage_module.build_stage_table_name("gp", "sandbox.target")
    with pytest.raises(ValueError, match="Invalid target table"):
        stage_module.build_stage_table_prefix("gp", "sandbox.target", None)

    valid_target = stage_module.exp.Table(this=stage_module.exp.Identifier(this="target"))
    parsed = iter([valid_target, object()])
    monkeypatch.setattr(stage_module, "parse_one", lambda *_args, **_kwargs: next(parsed))
    with pytest.raises(ValueError, match="Invalid transfer_staging_schema"):
        stage_module.build_stage_table_name(
            "gp",
            "sandbox.target",
            transfer_staging_schema="scratch",
        )
