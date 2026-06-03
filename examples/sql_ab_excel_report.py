"""Build an AB metrics Excel report from SQL-loaded or local data.

Run directly to generate a small demo report in /tmp without opening a
database connection. In production, replace ``load_demo_data`` with the
``read_sql`` call shown below.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from analytics_toolkit.ab_utils import (
    RatioMetricSpec,
    compute_test_metrics,
    format_ab_metrics,
)
from analytics_toolkit.excel import break_table
from analytics_toolkit.sql import read_sql


EXPERIMENT_SQL = """
select
    dt,
    user_id,
    group_name,
    segment,
    orders,
    gmv,
    clicks,
    impressions
from mart.ab_daily_user_metrics
where dt between date '2026-06-01' and date '2026-06-07'
"""


def load_experiment_data_from_sql(db_key: str) -> pd.DataFrame:
    return read_sql(db_key, EXPERIMENT_SQL)


def load_demo_data() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "dt": ["2026-06-01"] * 8,
            "user_id": range(1, 9),
            "group_name": [
                "control",
                "control",
                "control",
                "control",
                "test_1",
                "test_1",
                "test_1",
                "test_1",
            ],
            "segment": ["new", "new", "loyal", "loyal"] * 2,
            "orders": [1, 0, 2, 1, 1, 1, 3, 2],
            "gmv": [500.0, 0.0, 1200.0, 800.0, 700.0, 300.0, 1400.0, 900.0],
            "clicks": [5, 2, 8, 4, 6, 3, 9, 5],
            "impressions": [100, 80, 150, 120, 110, 90, 160, 130],
        }
    )


def build_report(input_df: pd.DataFrame, output: str | Path) -> pd.DataFrame:
    report_parts: list[pd.DataFrame] = []
    for (dt, segment), segment_df in input_df.groupby(["dt", "segment"], sort=False):
        metrics = compute_test_metrics(
            segment_df.drop(columns=["dt", "segment"]),
            group="group_name",
            user_id="user_id",
            control="control",
            ratio_metrics=[
                RatioMetricSpec(
                    name="ctr_user",
                    numerator="clicks",
                    denominator="impressions",
                    level="user",
                )
            ],
            test_vs_test=False,
        )
        metrics["dt"] = dt
        metrics["segment"] = segment
        report_parts.append(metrics)

    formatted = format_ab_metrics(
        pd.concat(report_parts, ignore_index=True),
        label_cols=["dt", "segment"],
        output_type=["metric_values", "p_values", "delta_relative"],
    )
    break_table(formatted, output=output, sheet_by="dt", break_by="segment", prettify=True)
    return formatted


if __name__ == "__main__":
    build_report(load_demo_data(), "/tmp/analytics_toolkit_ab_report.xlsx")
