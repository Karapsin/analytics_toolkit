from types import SimpleNamespace

import pytest
from analytics_toolkit.sql.backends import get_backend_adapter


@pytest.mark.parametrize(
    ("columns", "allowed"),
    [
        (["id", "default_value", "by_default_identity"], True),
        (["computed"], False),
        (["always_identity"], False),
    ],
)
def test_gp_generated_columns_are_rejected_before_insert(columns, allowed):
    statements = []
    closed = []

    def execute(sql, params):
        statements.append((sql, params))

    cursor = SimpleNamespace(
        execute=execute,
        fetchall=lambda: [
            ("id", True, False, "", ""),
            ("default_value", True, True, "", ""),
            ("by_default_identity", True, False, "", "d"),
            ("computed", False, False, "s", ""),
            ("always_identity", True, False, "", "a"),
        ],
        close=lambda: closed.append(True),
    )
    connection = SimpleNamespace(cursor=lambda: cursor)
    adapter = get_backend_adapter("gp")
    if allowed:
        adapter.validate_existing_dataframe_insert_columns(
            connection, '"schema.with.dot"."events"', columns, connection_key="gp"
        )
    else:
        with pytest.raises(ValueError, match="cannot be inserted explicitly"):
            adapter.validate_existing_dataframe_insert_columns(
                connection, '"schema.with.dot"."events"', columns, connection_key="gp"
            )
    assert len(statements) == 1
    sql, params = statements[0]
    assert params == ("schema.with.dot", "events")
    assert "COALESCE(row_to_json(a)->>'attgenerated', '') <> ''" in sql
    assert "COALESCE(row_to_json(a)->>'attidentity', '') = 'a'" in sql
    assert "a.attgenerated" not in sql
    assert "a.attidentity" not in sql
    assert closed == [True]


def test_gp_insertability_preserves_catalog_failure_and_closes_cursor():
    closed = []

    def execute(_sql, _params):
        message = "catalog unavailable"
        raise ConnectionError(message)

    cursor = SimpleNamespace(execute=execute, close=lambda: closed.append(True))
    with pytest.raises(ConnectionError, match="catalog unavailable"):
        get_backend_adapter("gp").validate_existing_dataframe_insert_columns(
            SimpleNamespace(cursor=lambda: cursor), "public.events", ["id"], connection_key="gp"
        )
    assert closed == [True]
