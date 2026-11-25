from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest
from analytics_toolkit.sql.backends import adapter_defaults, get_backend_adapter
from analytics_toolkit.sql.backends.models import SourceColumn, StageFinalizationRequest
from analytics_toolkit.sql.backends.source_schema import nullable_clickhouse_type


@pytest.mark.parametrize(("native", "expected"), [("tinyint", "TINYINT"), ("smallint", "SMALLINT")])
def test_portable_integer_spelling_uses_trino_native_family(native, expected):
    assert (
        get_backend_adapter("trino").map_source_type_to_target(SourceColumn("id", native))
        == expected
    )


@pytest.mark.parametrize(
    "native", ["Array(Int64)", "Tuple(Int64, String)", "Map(String, Int64)", "Nested(id Int64)"]
)
def test_nested_clickhouse_types_never_receive_an_illegal_outer_nullable(native):
    assert nullable_clickhouse_type(native) == native


def test_low_cardinality_nullable_preserves_the_dictionary_wrapper():
    assert nullable_clickhouse_type("LowCardinality(String)") == "LowCardinality(Nullable(String))"


def test_default_native_mapper_and_gp_transactional_error_policy_remain_compatible():
    adapter = get_backend_adapter("gp")
    assert (
        adapter_defaults.map_same_backend_source_type_to_target(
            adapter, SourceColumn("id", "integer")
        )
        == "INTEGER"
    )
    error = ConnectionError("transaction failed before acknowledgement")
    adapter.mark_upsert_finalization_error(error)
    assert not hasattr(error, "analytics_toolkit_sql_retry_safe")


def test_default_writable_schema_and_unsupported_replace_placeholder(monkeypatch):
    adapter = get_backend_adapter("trino")
    monkeypatch.setattr(adapter, "get_table_column_types", lambda *_a, **_k: {"id": "BIGINT"})
    assert adapter.get_writable_table_column_types(
        object(), "sandbox.events", connection_key="trino"
    ) == {"id": "BIGINT"}
    with pytest.raises(ValueError, match="does not support replace-style"):
        adapter.build_create_table_placeholder_sql("sandbox.events", replace_table=True)


def test_new_target_finalization_applies_mode_before_creating_and_copying(monkeypatch):
    adapter = get_backend_adapter("gp")
    events = []
    original = adapter.apply_target_write_mode

    def apply(request):
        events.append("mode")
        return original(request)

    monkeypatch.setattr(adapter, "apply_target_write_mode", apply)
    monkeypatch.setattr(adapter, "ensure_stage_target_table", lambda _r: events.append("create"))
    monkeypatch.setattr(adapter, "insert_from_table", lambda *_a, **_k: events.append("copy"))
    adapter.finalize_stage_table(
        StageFinalizationRequest(
            connection=SimpleNamespace(),
            stage_table="sandbox.stage",
            target_table="sandbox.events",
            replace_target_table=True,
            target_exists=False,
            sample_batch=pd.DataFrame({"id": [1]}),
            target_column_types={"id": "BIGINT"},
            write_mode="append",
        )
    )
    assert events == ["mode", "create", "copy"]
