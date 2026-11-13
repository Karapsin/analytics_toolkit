from types import SimpleNamespace

import pytest
from analytics_toolkit.sql.backends import get_backend_adapter

_METADATA = [
    ("id", "UInt64", ""),
    ("value", "String", "DEFAULT"),
    ("computed", "UInt64", "MATERIALIZED"),
    ("alias", "UInt64", "ALIAS"),
    ("ephemeral", "String", "EPHEMERAL"),
]
_SETTING_QUERY = "SELECT getSetting('insert_allow_materialized_columns')"


def _client(*, native, setting_rows, metadata=None):
    queries = []

    def query(sql):
        queries.append(sql)
        if sql.startswith("DESCRIBE TABLE"):
            return SimpleNamespace(result_rows=_METADATA if metadata is None else metadata)
        assert sql == _SETTING_QUERY
        return SimpleNamespace(result_rows=setting_rows)

    return SimpleNamespace(is_native_transport=native, query=query), queries


@pytest.mark.parametrize(
    ("native", "column", "setting", "allowed"),
    [
        (False, "alias", 1, False),
        (True, "alias", 1, False),
        (False, "computed", 0, False),
        (False, "computed", 1, False),
        (True, "computed", 0, False),
        (True, "computed", False, False),
        (True, "computed", "0", False),
        (True, "computed", 1, True),
        (True, "computed", True, True),
        (True, "computed", "1", True),
    ],
)
def test_clickhouse_explicit_insertability_respects_transport_and_effective_setting(
    native,
    column,
    setting,
    allowed,
):
    client, queries = _client(native=native, setting_rows=[(setting,)])
    adapter = get_backend_adapter("ch")
    if allowed:
        adapter.validate_existing_dataframe_insert_columns(
            client, "db.events", ["id", column], connection_key="ch"
        )
    else:
        with pytest.raises(ValueError, match="cannot be inserted explicitly"):
            adapter.validate_existing_dataframe_insert_columns(
                client, "db.events", ["id", column], connection_key="ch"
            )
    assert queries[0] == "DESCRIBE TABLE `db`.`events`"
    assert queries[1:] == ([_SETTING_QUERY] if native and column == "computed" else [])


@pytest.mark.parametrize("native", [False, True])
def test_clickhouse_ephemeral_and_default_inputs_remain_insertable(native):
    client, queries = _client(native=native, setting_rows=[])
    get_backend_adapter("ch").validate_existing_dataframe_insert_columns(
        client,
        "`db.with.dot`.`events`",
        ["ephemeral", "value", "id"],
        connection_key="ch",
    )
    assert queries == ["DESCRIBE TABLE `db.with.dot`.`events`"]


@pytest.mark.parametrize("rows", [[], [(None,)], [(2,)], [(1, 2)], [(1,), (0,)]])
def test_clickhouse_materialized_preflight_rejects_uncertifiable_effective_settings(rows):
    client, queries = _client(native=True, setting_rows=rows)
    with pytest.raises(ValueError, match="Cannot verify"):
        get_backend_adapter("ch").validate_existing_dataframe_insert_columns(
            client, "events", ["computed"], connection_key="ch"
        )
    assert queries == ["DESCRIBE TABLE `events`", _SETTING_QUERY]


@pytest.mark.parametrize(
    "metadata",
    [[], [("id", "UInt64")], [("id", "UInt64", "UNKNOWN")]],
)
def test_clickhouse_insertability_requires_complete_column_kind_metadata(metadata):
    client, queries = _client(native=True, setting_rows=[], metadata=metadata)
    with pytest.raises(ValueError, match="Cannot verify"):
        get_backend_adapter("ch").validate_existing_dataframe_insert_columns(
            client, "events", ["id"], connection_key="ch"
        )
    assert queries == ["DESCRIBE TABLE `events`"]


def test_clickhouse_insertability_preserves_metadata_failure():
    def query(_sql):
        message = "metadata unavailable"
        raise ConnectionError(message)

    with pytest.raises(ConnectionError, match="metadata unavailable"):
        get_backend_adapter("ch").validate_existing_dataframe_insert_columns(
            SimpleNamespace(query=query), "events", ["id"], connection_key="ch"
        )
