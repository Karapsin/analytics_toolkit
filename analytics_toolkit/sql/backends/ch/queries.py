from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..utils import sql_in_list, user_filter


def show_queries_sqls(
    self: Any,
    *,
    user: str | None,
    states: Sequence[str],
) -> list[dict[str, Any]]:
    del self
    queries: list[dict[str, Any]] = []
    user_sql = user_filter("user", "currentUser()", user)
    if "active" in states:
        queries.append(
            {
                "sql": _active_queries_sql(user_sql),
                "history": False,
            }
        )

    history_states = [state for state in states if state != "active"]
    if history_states:
        queries.append(
            {
                "sql": _history_queries_sql(user_sql, history_states),
                "history": True,
            }
        )
    return queries


def _active_queries_sql(user_sql: str) -> str:
    return f"""select
    query_id,
    user,
    'active' as state,
    query,
    null as started_at,
    null as finished_at,
    elapsed as elapsed_seconds,
    null as source,
    null as database,
    'active' as raw_state
from system.processes
where {user_sql}
  and query_id != currentQueryID()"""


def _history_queries_sql(user_sql: str, states: list[str]) -> str:
    type_values: list[str] = []
    if "finished" in states:
        type_values.append("QueryFinish")
    if "failed" in states:
        type_values.extend(["ExceptionBeforeStart", "ExceptionWhileProcessing"])
    return f"""select
    query_id,
    user,
    case
        when type = 'QueryFinish' then 'finished'
        else 'failed'
    end as state,
    query,
    query_start_time as started_at,
    event_time as finished_at,
    query_duration_ms / 1000.0 as elapsed_seconds,
    null as source,
    current_database as database,
    type as raw_state
from system.query_log
where {user_sql}
  and {sql_in_list("type", type_values)}
  and query_id != currentQueryID()"""


__all__ = ["show_queries_sqls"]
