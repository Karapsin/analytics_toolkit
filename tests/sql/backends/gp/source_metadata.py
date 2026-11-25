"""DB-API source identities retain native GP families and unbounded numerics."""

from __future__ import annotations

import pytest
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.models import SourceColumn
from analytics_toolkit.sql.backends.source_schema import map_source_column


@pytest.mark.parametrize(
    ("oid", "expected"),
    [(114, "json"), (869, "inet"), (650, "cidr"), (1186, "interval"), (1187, "interval[]")],
)
def test_gp_source_oid_keeps_supported_native_identity(oid, expected):
    assert get_backend_adapter("gp").type_code_name(oid, None, None) == expected


@pytest.mark.parametrize("oid", [1700, 1231])
def test_gp_unbounded_numeric_sentinels_do_not_become_invalid_typmods(oid):
    expected = "numeric[]" if oid == 1231 else "numeric"
    assert get_backend_adapter("gp").type_code_name(oid, 65535, 65535) == expected


@pytest.mark.parametrize("native", ["inet", "cidr", "time", "time(6)", "interval"])
def test_gp_inferred_native_source_mapping_keeps_supported_family(native):
    assert map_source_column(SourceColumn("id", native), "gp", "gp").upper() == native.upper()
