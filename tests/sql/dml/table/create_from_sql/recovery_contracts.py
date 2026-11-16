from __future__ import annotations

from decimal import Decimal

import pandas as pd
import pytest
from analytics_toolkit.sql.ddl.ownership import CreationOwnership
from analytics_toolkit.sql.dml.load.load_sql_table import _logged_insert_exception
from analytics_toolkit.sql.dml.load.type_validation import validate_dataframe_schema
from analytics_toolkit.sql.dml.table.create_ownership import require_created_target_acknowledgement
from analytics_toolkit.sql.dml.table.table_validation import validate_complete_upsert_columns

from tests.sql._support.create_from_sql import create_module


def test_unacknowledged_target_cannot_be_populated_but_explicit_replacement_is_allowed():
    ownership = CreationOwnership(submitted=True, pending_resource="sandbox.target")
    with pytest.raises(RuntimeError, match="without an ownership acknowledgement"):
        require_created_target_acknowledgement(ownership, replace=False)
    require_created_target_acknowledgement(ownership, replace=True)
    assert ownership.resources == []


def test_duplicate_full_upsert_columns_are_rejected_even_when_all_names_exist():
    with pytest.raises(ValueError, match="duplicate input columns"):
        validate_complete_upsert_columns(["id", "value", "id"], {"id": "INTEGER", "value": "TEXT"})


@pytest.mark.parametrize("backend", ["gp", "trino"])
def test_fractional_float_without_an_int_conversion_cannot_be_truncated(backend):
    with pytest.raises(ValueError, match="incompatible with authoritative integer schema"):
        validate_dataframe_schema(pd.DataFrame({"id": [1.25]}), {"id": "INTEGER"}, backend)
    # ClickHouse's deliberate native int(value) assignment is retained.
    validate_dataframe_schema(pd.DataFrame({"id": [1.25]}), {"id": "Int64"}, "ch")
    validate_dataframe_schema(pd.DataFrame({"id": [1.25]}), {"id": "Float64"}, "ch")
    validate_dataframe_schema(pd.DataFrame({"id": [Decimal("1.25")]}), {"id": "Decimal(3,2)"}, "ch")


def test_safe_insert_error_bounds_untrusted_exception_type_names():
    error_class = type("RemoteBackend" * 30, (RuntimeError,), {})
    text = _logged_insert_exception(error_class("private input value"), safe=True)
    assert text.endswith("... (details suppressed)")
    assert "private input value" not in text
    assert len(text) < 180


def test_unacknowledged_target_has_no_cleanup_authority():
    options = create_module.CreateTableFromSqlOptions(
        "gp", "gp", "gp", "gp", "sandbox.target", "SELECT 1"
    )
    assert (
        create_module._cleanup_attempt_target(
            options=options,
            target_adapter=None,
            target_connection=None,
        )
        is False
    )
