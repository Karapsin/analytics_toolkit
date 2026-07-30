from __future__ import annotations

import os
import uuid
from typing import TYPE_CHECKING

import pandas as pd
import pytest
from analytics_toolkit import sql, sql_format
from tests.integration.support.backends import integration_table
from tests.integration.support.identity import resource_name

if TYPE_CHECKING:
    from tests.integration.support.resources import ResourceRegistry

pytestmark = [pytest.mark.integration, pytest.mark.integration_core]


def _require_greenplum() -> None:
    if os.environ.get("SQL_INTEGRATION_GP") != "1":
        pytest.skip("Greenplum integration runs only on x86_64")


def _temp_name(purpose: str) -> str:
    run_id = os.environ.get("SQL_INTEGRATION_RUN_ID", uuid.uuid4().hex[:8])
    test_id = os.environ.get("SQL_INTEGRATION_TEST_ID", "manual")
    return resource_name(run_id, test_id, purpose)


def _register_source(
    resource_registry: ResourceRegistry,
    purpose: str,
) -> str:
    return resource_registry.table("gp", integration_table("gp", purpose))


def _register_temp(
    resource_registry: ResourceRegistry,
    purpose: str,
) -> str:
    return resource_registry.table("gp", _temp_name(purpose))


def _load_source(table: str, frame: pd.DataFrame, key: str) -> None:
    sql.load_df(
        "gp",
        table,
        frame,
        write_mode="replace",
        gp_distributed_by_key=key,
    )


@pytest.mark.sql_scenario("format.rewrite_equivalence.gp.report_ctes")
def test_gp_rewrite_preserves_report_query_results(
    resource_registry: ResourceRegistry,
) -> None:
    _require_greenplum()
    users_table = _register_source(resource_registry, "format_users")
    cheques_table = _register_source(resource_registry, "format_cheques")
    items_table = _register_source(resource_registry, "format_items")
    supplier_table = _register_source(resource_registry, "format_suppliers")
    articles_table = _register_source(resource_registry, "format_articles")
    promo_table = _register_source(resource_registry, "format_promos")

    _load_source(
        users_table,
        pd.DataFrame(
            {
                "contact_id": [1, 2, 3],
                "mandatory_user_flg": [0, 1, 0],
            }
        ),
        "contact_id",
    )
    _load_source(
        cheques_table,
        pd.DataFrame(
            {
                "contact_id": [1, 1, 2, 3],
                "cheque_pk": [101, 102, 103, 104],
                "datetime": pd.to_datetime(
                    [
                        "2026-01-02 10:00:00",
                        "2026-01-03 10:00:00",
                        "2026-01-04 10:00:00",
                        "2026-01-08 00:00:00",
                    ]
                ),
                "operation_type_id": [1, 2, 1, 1],
            }
        ),
        "cheque_pk",
    )
    _load_source(
        items_table,
        pd.DataFrame(
            {
                "cheque_pk": [101, 101, 101, 102, 103, 104],
                "article_id": [10, 10, 20, 10, 10, 10],
                "summ_discounted": [5.0, 7.0, 3.0, 50.0, 9.0, 100.0],
                "quantity": [1.0, 2.0, 1.0, 5.0, 1.0, 10.0],
                "datetime": pd.to_datetime(
                    [
                        "2026-01-02 10:00:00",
                        "2026-01-02 10:00:00",
                        "2026-01-02 10:00:00",
                        "2026-01-03 10:00:00",
                        "2026-01-04 10:00:00",
                        "2026-01-08 00:00:00",
                    ]
                ),
            }
        ),
        "cheque_pk",
    )
    _load_source(
        supplier_table,
        pd.DataFrame({"article_id": [10, 10]}),
        "article_id",
    )
    _load_source(
        articles_table,
        pd.DataFrame(
            {
                "article_id": [10, 20, 30],
                "code": ["supplier-overlap", "promo-20", "unused"],
            }
        ),
        "article_id",
    )
    _load_source(
        promo_table,
        pd.DataFrame({"mass_promo_code": ["supplier-overlap", "promo-20"]}),
        "mass_promo_code",
    )

    users_cte = _register_temp(resource_registry, "format_cte_users")
    cheques_cte = _register_temp(resource_registry, "format_cte_cheques")
    articles_cte = _register_temp(resource_registry, "format_cte_articles")
    items_cte = _register_temp(resource_registry, "format_cte_items")
    query = f"""
        with {users_cte} as (
            select distinct contact_id
            from {users_table}
            where mandatory_user_flg = 0
        ),
        {cheques_cte} as (
            select t1.contact_id, t1.cheque_pk
            from {cheques_table} as t1
            where t1.datetime >= timestamp '2026-01-01'
              and t1.datetime < timestamp '2026-01-08'
              and t1.operation_type_id = 1
        ),
        {articles_cte} as (
            select article_id
            from {supplier_table}
            union
            select t1.article_id
            from {articles_table} as t1
            join {promo_table} as t2
              on t1.code = t2.mass_promo_code::text
        ),
        {items_cte} as (
            select
                t2.contact_id,
                t1.article_id,
                sum(t1.summ_discounted) as volume,
                sum(t1.quantity) as quantity,
                count(distinct t1.cheque_pk) as cheques_num
            from {items_table} as t1
            join {cheques_cte} as t2
              on t1.cheque_pk = t2.cheque_pk
            join {articles_cte} as t3
              on t1.article_id = t3.article_id
            where t1.datetime >= timestamp '2026-01-01'
              and t1.datetime < timestamp '2026-01-08'
            group by 1, 2
        )
        select
            date '2026-01-01' as dt,
            contact_id,
            article_id,
            volume,
            quantity,
            cheques_num
        from {items_cte}
        order by contact_id, article_id
    """

    original = sql.read("gp", query)
    rewritten = sql_format.gp_rewrite_to_temp_tables(query)
    actual = sql.execute_read("gp", rewritten, gp_break_query=True)

    pd.testing.assert_frame_equal(actual, original)


@pytest.mark.sql_scenario("format.rewrite_equivalence.gp.set_operations")
def test_gp_rewrite_preserves_set_operation_results(
    resource_registry: ResourceRegistry,
) -> None:
    _require_greenplum()
    left_table = _register_source(resource_registry, "format_set_left")
    right_table = _register_source(resource_registry, "format_set_right")
    _load_source(left_table, pd.DataFrame({"value": [1, 1, 2, 4]}), "value")
    _load_source(right_table, pd.DataFrame({"value": [1, 3, 4, 4]}), "value")

    for suffix, operator in (
        ("union_all", "union all"),
        ("intersect", "intersect"),
        ("except", "except"),
    ):
        cte_name = _register_temp(resource_registry, f"format_cte_{suffix}")
        query = (
            f"with {cte_name} as ("
            f"select value from {left_table} {operator} "
            f"select value from {right_table}"
            f") select value from {cte_name} order by value"
        )

        original = sql.read("gp", query)
        rewritten = sql_format.gp_rewrite_to_temp_tables(query)
        actual = sql.execute_read("gp", rewritten, gp_break_query=True)

        pd.testing.assert_frame_equal(actual, original)
