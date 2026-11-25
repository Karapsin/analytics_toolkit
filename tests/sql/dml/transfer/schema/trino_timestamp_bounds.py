"""A high-precision engine type alone cannot certify destination storage."""

from __future__ import annotations

from analytics_toolkit.sql.backends.calendar_ticks import days_from_civil
from analytics_toolkit.sql.core.exact_values import ExactTemporal

from tests.sql._support import adaptive_transfer as support


def test_public_transfer_profiles_complete_high_precision_domain_before_target_creation(
    monkeypatch,
):
    class Cursor:
        def execute(self, statement, params=None):
            assert "system.metadata.catalogs" in statement
            assert params == ["iceberg"]

        def fetchall(self):
            return [("memory",)]

        def close(self):
            pass

    class Connection(support.Connection):
        catalog = "iceberg"

        def cursor(self):
            return Cursor()

    monkeypatch.setattr(support, "Connection", Connection)
    wide = ExactTemporal(days_from_civil(10000, 1, 1) * 86400 * 10**9 + 1, 9, wall_time=True)
    scenario = support.install(
        monkeypatch,
        native_type="TIMESTAMP(9)",
        rows=[(ExactTemporal(0, 9, wall_time=True),), (wide,)],
        from_db_key="trino",
        from_db_backend="trino",
        to_db_key="trino",
        to_db_backend="trino",
        target_table="iceberg.sandbox.target",
        table_schema={"id": "TIMESTAMP(9)"},
    )
    scenario.existing_types = {"id": "TIMESTAMP(9)"}
    paths = support.track_spools(monkeypatch)

    assert scenario.run() == 2

    assert paths
    assert scenario.events.index("complete:SELECT id FROM source") < scenario.events.index(
        "create_target"
    )
    assert scenario.loaded[0][-1][0] == wide
    assert scenario.read_count == 1
    assert all(not path.exists() for path in paths)
