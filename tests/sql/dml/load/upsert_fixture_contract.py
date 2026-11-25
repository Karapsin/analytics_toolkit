"""The stateful upsert driver evaluates emitted identities and relational SQL."""

from __future__ import annotations

from tests.sql._support.upsert_scope import INCOMING_ROWS, StatefulUpsertClient


def test_upsert_state_driver_distinguishes_database_names_in_cluster_sources():
    client = StatefulUpsertClient(local=True)
    client.rows[("caller_a", "other", "incoming")] = [(9, 202405, 99)]

    result = client.query("SELECT id, month, value FROM cluster('caller_a', 'other', 'incoming')")

    assert result.result_rows == [(9, 202405, 99)]
    assert client.rows[("caller_a", "incoming")] == INCOMING_ROWS


def test_upsert_state_driver_evaluates_the_actual_join_predicate():
    client = StatefulUpsertClient(local=True)

    result = client.query(
        "SELECT saved.id, saved.month, saved.value FROM db.events AS saved "
        "INNER JOIN cluster('caller_a', 'db', 'incoming') AS incoming "
        "ON saved.month <> incoming.month"
    )

    assert result.result_rows == [
        (1, 202401, 10),
        (2, 202401, 20),
        (1, 202402, 30),
        (3, 202403, 40),
        (3, 202403, 40),
    ]


def test_upsert_state_driver_applies_explicit_destination_column_order():
    client = StatefulUpsertClient(local=True)

    client.command(
        "INSERT INTO FUNCTION cluster('caller_a', 'db', 'payload') "
        "(value, month, id) SELECT id, month, value "
        "FROM cluster('caller_a', 'db', 'incoming')"
    )

    assert client.rows[("caller_a", "payload")] == [(11, 202401, 1), (31, 202402, 1)]
    assert client.rows[("caller_a", "incoming")] == INCOMING_ROWS


def test_upsert_state_driver_evaluates_grouped_having_instead_of_returning_empty():
    client = StatefulUpsertClient(local=True)
    client.rows[("caller_a", "incoming")] = [INCOMING_ROWS[0], INCOMING_ROWS[0]]

    result = client.query(
        "SELECT id, month, count(*) AS n FROM cluster('caller_a', 'db', 'incoming') "
        "GROUP BY id, month HAVING count(*) > 1"
    )

    assert result.result_rows == [(1, 202401, 2)]
