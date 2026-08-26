from __future__ import annotations

from tests.sql._support.adapters import (
    GP_IDENTIFIER_MAX_BYTES,
    get_backend_adapter,
    gp_stage_module,
    pytest,
)


def test_gp_identifier_byte_helpers_cover_tiny_and_multibyte_limits() -> None:
    assert gp_stage_module._fit_identifier_bytes("very-long-name", 4) == "458f"
    assert gp_stage_module._truncate_identifier_bytes("short", 10) == "short"
    assert (
        gp_stage_module._truncate_identifier_bytes(
            "\u0430\u0431\u0432",
            5,
        )
        == "\u0430\u0431"
    )


def test_gp_stage_base_identifier_keeps_marker_within_backend_limit() -> None:
    adapter = get_backend_adapter("gp")

    identifier = adapter.stage_base_identifier(
        "very_long_target_table_name_for_monthly_analytics_exports",
        "karapsin_de",
        "4f99601c",
    )
    stage_identifier = f"{identifier}__analytics_toolkit_karapsin_de__stage__4f99601c"

    assert len(stage_identifier.encode()) <= GP_IDENTIFIER_MAX_BYTES
    assert stage_identifier.endswith("__stage__4f99601c")


def test_gp_stage_identifier_rejects_marker_larger_than_identifier_limit() -> None:
    with pytest.raises(ValueError, match="marker is too long"):
        get_backend_adapter("gp").stage_base_identifier(
            "target",
            "x" * GP_IDENTIFIER_MAX_BYTES,
            "suffix",
        )


def test_greenplum_upsert_finalizes_every_incoming_stage() -> None:
    adapter = get_backend_adapter("gp")
    sqls = adapter.build_upsert_stage_sqls(
        "target",
        "stage_a",
        columns=["id", "value"],
        key_columns=["id"],
        incoming_stage_tables=["stage_a", "stage_b"],
    )

    assert len(sqls) == 4
    assert all("stage_a" in sql for sql in sqls[:2])
    assert all("stage_b" in sql for sql in sqls[2:])
