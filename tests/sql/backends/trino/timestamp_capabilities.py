"""Connector receipts certify wide logical values before publication or replay."""

from __future__ import annotations

import pytest
from analytics_toolkit.sql.backends.calendar_ticks import days_from_civil
from analytics_toolkit.sql.backends.native_values import (
    require_timestamp_write_capabilities,
    timestamp_capability_plan_options,
)
from analytics_toolkit.sql.backends.trino.timestamp_capabilities import (
    require_timestamp_capabilities,
    timestamp_capability_options,
)
from analytics_toolkit.sql.backends.value_profiles import ColumnProfile
from analytics_toolkit.sql.core.exact_values import ExactTemporal


class _Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.statement = ""
        self.closed = False

    def execute(self, statement, parameters=None):
        self.statement = statement
        self.connection.statements.append((statement, parameters))
        if self.connection.failure is not None:
            raise self.connection.failure

    def fetchall(self):
        if "system.metadata.catalogs" in self.statement:
            return self.connection.catalog_rows
        assert "SHOW CREATE TABLE" in self.statement
        return self.connection.ddl_rows

    def close(self):
        self.closed = True
        if self.connection.close_failure:
            message = "cursor close failed"
            raise RuntimeError(message)


class _Connection:
    catalog = "AlIaS"
    schema = "schema.with.dot"

    def __init__(self, connector="memory", *, ddl=None):
        self.catalog_rows = [(connector,)]
        self.ddl_rows = [
            (ddl or "CREATE TABLE alias.s.target (value TIMESTAMP(9)) WITH (format_version = 3)",)
        ]
        self.statements = []
        self.cursors = []
        self.failure = None
        self.close_failure = False

    def cursor(self):
        cursor = _Cursor(self)
        self.cursors.append(cursor)
        return cursor


def _wide_value(precision=9):
    return ExactTemporal(
        days_from_civil(10000, 1, 1) * 86400 * 10**precision + 1, precision, wall_time=True
    )


def _profile(value):
    profile = ColumnProfile()
    profile.add(value)
    return profile


def _require(connection, *, precision=9, cache=None, **options):
    return require_timestamp_capabilities(
        connection,
        "target",
        {"value": f"TIMESTAMP({precision})"},
        {"value": _profile(_wide_value(precision))},
        {} if cache is None else cache,
        **options,
    )


def test_memory_receipt_uses_actual_connector_and_rechecks_fresh_connections():
    first, second = _Connection(), _Connection()
    cache = {"pending_timestamp_capabilities": {"pending": True}}
    receipt = _require(first, cache=cache)
    assert receipt == {
        "connector": "memory",
        "format_version": None,
        "timestamp_storage": "trino-native",
        "columns": ["value"],
    }
    assert first.statements[0][1] == ["alias"]
    assert len(first.statements) == 1
    assert _require(first, cache=cache) == receipt
    assert len(first.statements) == 1
    assert _require(second, cache=cache) == receipt
    assert len(second.statements) == 1
    assert "pending_timestamp_capabilities" not in cache
    assert all(cursor.closed for connection in (first, second) for cursor in connection.cursors)
    with pytest.raises(ValueError, match="capabilities changed"):
        _require(_Connection("iceberg"), cache=cache)


def test_existing_storage_metadata_quotes_complete_identifier_components():
    connection = _Connection("iceberg")
    with pytest.raises(ValueError, match="signed64 nanosecond"):
        _require(connection)
    statement, parameters = connection.statements[-1]
    assert 'SHOW CREATE TABLE "AlIaS"."schema.with.dot"."target"' in statement
    assert parameters is None
    assert all(cursor.closed for cursor in connection.cursors)


@pytest.mark.parametrize("version", [3, "3"])
def test_desired_iceberg_properties_are_checked_without_show_create_or_format_migration(version):
    connection = _Connection("iceberg")
    with pytest.raises(ValueError, match="signed64 nanosecond"):
        _require(connection, creating=True, properties={"format_version": version})
    assert len(connection.statements) == 1
    assert "system.metadata.catalogs" in connection.statements[0][0]


@pytest.mark.parametrize("version", [None, 2, True, "three", "("])
def test_missing_or_unverifiable_iceberg_format_never_certifies_wide_values(version):
    with pytest.raises(ValueError, match="verified format-v3"):
        _require(_Connection("iceberg"), creating=True, properties={"format_version": version})


def test_native_iceberg_nanosecond_type_is_required_for_high_precision_storage():
    with pytest.raises(ValueError, match="precision nine"):
        _require(_Connection("iceberg"), precision=12)


@pytest.mark.parametrize("rows", [[], [()], [(None,)], [("memory",), ("memory",)]])
def test_incomplete_connector_metadata_is_actionable_before_any_storage_assumption(rows):
    connection = _Connection()
    connection.catalog_rows = rows
    with pytest.raises(ValueError, match="connector metadata access"):
        _require(connection)
    assert all(cursor.closed for cursor in connection.cursors)


def test_an_unknown_connector_is_not_inferred_from_the_catalog_alias():
    with pytest.raises(ValueError, match="this connector"):
        _require(_Connection("other_connector"))


@pytest.mark.parametrize("rows", [[], [()], [(None,)], [("SELECT 1",)], [("CREATE TABLE (",)]])
def test_incomplete_or_unparseable_show_create_never_certifies_existing_storage(rows):
    connection = _Connection("iceberg")
    connection.ddl_rows = rows
    with pytest.raises(ValueError, match=r"table properties|table metadata"):
        _require(connection)
    assert all(cursor.closed for cursor in connection.cursors)


def test_existing_table_without_a_verified_format_version_remains_uncertified():
    connection = _Connection("iceberg", ddl="CREATE TABLE alias.s.target (value TIMESTAMP(9))")
    with pytest.raises(ValueError, match="verified format-v3"):
        _require(connection)


def test_capability_queries_preserve_primary_failures_when_cursor_close_also_fails(caplog):
    connection = _Connection()
    failure = RuntimeError("metadata request failed")
    connection.failure = failure
    connection.close_failure = True
    with pytest.raises(RuntimeError, match="metadata request failed") as error:
        _require(connection)
    assert error.value is failure
    assert connection.cursors[0].closed
    assert "Failed to close Trino timestamp capability metadata cursor" in caplog.text


def test_close_failure_does_not_invalidate_successful_connector_certification(caplog):
    connection = _Connection()
    connection.close_failure = True
    assert _require(connection)["connector"] == "memory"
    assert "Failed to close Trino timestamp capability metadata cursor" in caplog.text


def test_nested_logical_profiles_drive_capability_plans_instead_of_physical_text_types():
    schema = {"payload": 'ARRAY(ROW("when" TIMESTAMP(12)))', "missing": "VARCHAR"}
    profiles = {"payload": _profile([(_wide_value(12),)])}
    pending = timestamp_capability_options(schema, profiles)
    assert pending["pending_timestamp_capabilities"]["columns"] == ["payload"]
    assert timestamp_capability_options(schema)["pending_timestamp_capabilities"]["columns"] == [
        "payload"
    ]
    receipt = require_timestamp_capabilities(_Connection(), "target", schema, profiles, {})
    assert receipt["columns"] == ["payload"]
    assert timestamp_capability_options({"value": "TIMESTAMP(6)"}) == {}


def test_backend_neutral_dispatch_has_no_metadata_side_effect_for_other_backends():
    schema, profiles = {"value": "TIMESTAMP(9)"}, {"value": _profile(_wide_value())}
    assert require_timestamp_write_capabilities("gp", None, "target", schema, profiles, {}) is None
    assert timestamp_capability_plan_options("gp", schema) == {}
    assert (
        require_timestamp_write_capabilities(
            "trino", _Connection(), "target", schema, profiles, {}
        )["connector"]
        == "memory"
    )
    assert timestamp_capability_plan_options("trino", schema)["pending_timestamp_capabilities"][
        "pending"
    ]


def test_wide_storage_certification_requires_a_resolved_relation_namespace():
    connection = _Connection()
    connection.catalog = connection.schema = None
    with pytest.raises(ValueError, match="Fully qualify"):
        _require(connection)
    assert connection.statements == []
