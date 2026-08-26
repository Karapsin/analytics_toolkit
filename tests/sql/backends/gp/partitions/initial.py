from __future__ import annotations

import importlib
from datetime import date, datetime, timezone
from typing import Any

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.gp import partitions as gp_partitions_module
from analytics_toolkit.sql.backends.models import StageTargetTableRequest
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.ddl.identifiers import quote_identifier

ddl_api = importlib.import_module("analytics_toolkit.sql.ddl.api")
load_module = importlib.import_module("analytics_toolkit.sql.dml.load.load_df")
transfer_module = importlib.import_module("analytics_toolkit.sql.dml.transfer.flow.api")


RANGE_PARTITIONS = {
    "start": "2025-01-01",
    "end": "2025-04-01",
    "interval": "1 month",
}


def test_create_sql_table_renders_exact_greenplum_range_partition_ddl() -> None:
    generated = sql.create_sql_table(
        "gp",
        "sandbox.events",
        table_schema={"event_date": "DATE", "id": "BIGINT"},
        gp_distributed_by_key="id",
        partition_by="event_date",
        gp_partitions=RANGE_PARTITIONS,
        only_generate_sql=True,
        query_label="partition_create",
    )

    assert generated == (
        "/* analytics_toolkit query_label=partition_create */\n"
        'CREATE TABLE sandbox.events ("event_date" DATE, '
        '"id" BIGINT) WITH (appendonly=true,\n'
        "        blocksize=32768,\n"
        "        compresstype=zstd,\n"
        "        compresslevel=4,\n"
        '        orientation=column) DISTRIBUTED BY ("id") '
        'PARTITION BY RANGE ("event_date")\n'
        "(\n"
        "    START ('2025-01-01') INCLUSIVE\n"
        "    END ('2025-04-01') EXCLUSIVE\n"
        "    EVERY (INTERVAL '1 month')\n"
        ")"
    )


def test_create_sql_table_renders_list_partitions_and_escapes_values() -> None:
    generated = sql.create_sql_table(
        "gp",
        "sandbox.accounts",
        pd.DataFrame({"segment": ["free"], "id": [1]}),
        partition_by="segment",
        gp_partitions={"values": ["free", "partner's"]},
        only_generate_sql=True,
    )

    assert 'PARTITION BY LIST ("segment")' in generated
    assert "PARTITION p_free VALUES ('free')" in generated
    assert "PARTITION p_partner_s VALUES ('partner''s')" in generated
    assert "DISTRIBUTED RANDOMLY" in generated


def test_create_sql_table_plan_and_metadata_keep_complete_partitioned_ddl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _RecordingConnection()
    monkeypatch.setattr(ddl_api, "get_sql_connection", lambda key: connection)

    plan = sql.create_sql_table(
        "gp",
        "sandbox.events",
        table_schema={"event_date": "DATE", "id": "BIGINT"},
        partition_by="event_date",
        gp_partitions=RANGE_PARTITIONS,
        dry_run=True,
    )
    assert plan.options["gp_partitions"] == RANGE_PARTITIONS
    assert "EVERY (INTERVAL '1 month')" in plan.sqls[0]

    result = sql.create_sql_table(
        "gp",
        "sandbox.events",
        table_schema={"event_date": "DATE", "id": "BIGINT"},
        partition_by="event_date",
        gp_partitions=RANGE_PARTITIONS,
        return_metadata=True,
        retry_cnt=1,
    )
    assert result.plan is not None
    assert result.plan.sqls == connection.executed


def test_load_df_replace_plan_partitions_only_the_final_target() -> None:
    plan = sql.load_df(
        "gp",
        "sandbox.events",
        pd.DataFrame({"event_date": [date(2025, 1, 2)], "id": [1]}),
        write_mode="replace",
        partition_by="event_date",
        gp_partitions=RANGE_PARTITIONS,
        dry_run=True,
    )

    assert plan.options["gp_partitions"] == RANGE_PARTITIONS
    partitioned = [statement for statement in plan.sqls if "PARTITION BY" in statement]
    assert len(partitioned) == 1
    assert "CREATE TABLE sandbox.events" in partitioned[0]


def test_transfer_plan_partitions_final_target_but_not_stage_tables() -> None:
    plan = sql.transfer(
        "gp",
        "gp_sandbox",
        from_sql="select event_date, id from source_events",
        to_table="sandbox.events",
        write_mode="replace",
        table_schema={"event_date": "DATE", "id": "BIGINT"},
        partition_by="event_date",
        gp_partitions=RANGE_PARTITIONS,
        dry_run=True,
    )

    assert plan.options["gp_partitions"] == RANGE_PARTITIONS
    partitioned = [statement for statement in plan.sqls if "PARTITION BY" in statement]
    assert len(partitioned) == 1
    assert "CREATE TABLE sandbox.events" in partitioned[0]


@pytest.mark.parametrize(
    ("gp_partitions", "match"),
    [
        ({}, "must not be empty"),
        ({"start": "2025-01-01", "end": "2025-02-01"}, "exactly"),
        (
            {
                "start": "2025-01-01",
                "end": "2025-02-01",
                "interval": "1 month",
                "extra": True,
            },
            "exactly",
        ),
        (
            {"start": "2025-03-01", "end": "2025-02-01", "interval": "1 month"},
            "after start",
        ),
        (
            {"start": "2025-01-01", "end": "2025-02-02", "interval": "1 month"},
            "reach end exactly",
        ),
        ({"values": []}, "non-empty sequence"),
        ({"values": ["free", "free"]}, "duplicates"),
        ({"values": ["a-b", "a b"]}, "unique partition names"),
        ({"values": ["ok", "bad\0value"]}, "NUL"),
    ],
)
def test_gp_partition_mapping_validation(
    gp_partitions: dict[str, Any],
    match: str,
) -> None:
    with pytest.raises(InvalidSqlInputError, match=match):
        sql.create_sql_table(
            "gp",
            "sandbox.events",
            table_schema={"event_date": "DATE"},
            partition_by="event_date",
            gp_partitions=gp_partitions,
            only_generate_sql=True,
        )


@pytest.mark.parametrize(
    "interval",
    [
        "0 days",
        "-1 day",
        "1.5 months",
        "1 hour",
        "1 month; DROP TABLE x",
        1,
    ],
)
def test_gp_partition_interval_rejects_unsafe_or_unsupported_values(
    interval: Any,
) -> None:
    with pytest.raises(InvalidSqlInputError, match="positive whole number"):
        sql.create_sql_table(
            "gp",
            "sandbox.events",
            table_schema={"event_date": "DATE"},
            partition_by="event_date",
            gp_partitions={
                "start": "2025-01-01",
                "end": "2025-02-01",
                "interval": interval,
            },
            only_generate_sql=True,
        )


def test_gp_partition_dates_accept_date_and_datetime() -> None:
    generated = sql.create_sql_table(
        "gp",
        "sandbox.events",
        table_schema={"event_date": "DATE"},
        partition_by=["event_date"],
        gp_partitions={
            "start": datetime(2025, 1, 1, 12, tzinfo=timezone.utc),
            "end": date(2025, 1, 15),
            "interval": "2 weeks",
        },
        only_generate_sql=True,
    )
    assert "START ('2025-01-01')" in generated
    assert "END ('2025-01-15')" in generated
    assert "INTERVAL '2 weeks'" in generated


def test_gp_partition_cross_option_and_backend_validation() -> None:
    with pytest.raises(InvalidSqlInputError, match="requires gp_partitions"):
        sql.create_sql_table(
            "gp",
            "sandbox.events",
            table_schema={"event_date": "DATE"},
            partition_by="event_date",
            only_generate_sql=True,
        )
    with pytest.raises(InvalidSqlInputError, match="requires partition_by"):
        sql.create_sql_table(
            "gp",
            "sandbox.events",
            table_schema={"event_date": "DATE"},
            gp_partitions=RANGE_PARTITIONS,
            only_generate_sql=True,
        )
    with pytest.raises(InvalidSqlInputError, match="exactly one column"):
        sql.create_sql_table(
            "gp",
            "sandbox.events",
            table_schema={"event_date": "DATE", "id": "BIGINT"},
            partition_by=["event_date", "id"],
            gp_partitions=RANGE_PARTITIONS,
            only_generate_sql=True,
        )
    for backend in ("trino", "ch"):
        with pytest.raises(InvalidSqlInputError, match="only be used"):
            sql.create_sql_table(
                backend,
                "sandbox.events",
                table_schema={"event_date": "DATE"},
                partition_by="event_date",
                gp_partitions=RANGE_PARTITIONS,
                only_generate_sql=True,
            )


@pytest.mark.parametrize(
    ("value", "partition_by", "match"),
    [
        (None, None, None),
        (["not", "a", "mapping"], "event_date", "must be a mapping"),
    ],
)
def test_gp_partition_normalizer_edge_inputs(
    value: Any,
    partition_by: str | None,
    match: str | None,
) -> None:
    if match is None:
        assert (
            gp_partitions_module.normalize_gp_partitions(
                value,
                partition_by=partition_by,
            )
            is None
        )
        return
    with pytest.raises(InvalidSqlInputError, match=match):
        gp_partitions_module.normalize_gp_partitions(
            value,
            partition_by=partition_by,
        )


@pytest.mark.parametrize("value", [b"event_date", 7])
def test_gp_partition_column_rejects_non_column_sequences(value: Any) -> None:
    with pytest.raises(InvalidSqlInputError, match="exactly one"):
        gp_partitions_module.normalize_gp_partition_column(value)


@pytest.mark.parametrize(
    ("values", "match"),
    [
        ("free", "non-empty sequence"),
        (7, "non-empty sequence"),
        ([7], "contain strings"),
        ([" "], "empty strings"),
    ],
)
def test_gp_list_partition_rejects_invalid_sequence_items(
    values: Any,
    match: str,
) -> None:
    with pytest.raises(InvalidSqlInputError, match=match):
        gp_partitions_module.normalize_gp_partitions(
            {"values": values},
            partition_by="segment",
        )


@pytest.mark.parametrize("value", [7, "not-a-date"])
def test_gp_range_partition_rejects_invalid_dates(value: Any) -> None:
    with pytest.raises(InvalidSqlInputError, match="ISO date"):
        gp_partitions_module.normalize_gp_partitions(
            {"start": value, "end": "2025-02-01", "interval": "1 month"},
            partition_by="event_date",
        )


def test_gp_partition_helpers_cover_empty_rendering_and_interval_units() -> None:
    assert (
        gp_partitions_module.render_gp_partition_clause(
            None,
            None,
            quote_identifier=quote_identifier,
        )
        == ""
    )
    assert gp_partitions_module.sanitize_gp_partition_name_token("---") == "partition"
    for end, interval in (
        ("2025-01-03", "2 days"),
        ("2027-01-01", "2 years"),
    ):
        spec = gp_partitions_module.normalize_gp_partitions(
            {"start": "2025-01-01", "end": end, "interval": interval},
            partition_by="event_date",
        )
        assert spec is not None
    with pytest.raises(AssertionError, match="Unexpected"):
        gp_partitions_module._increment_partition_date(date(2025, 1, 1), 1, "hour")


def test_gp_partition_compatibility_renderer_and_create_kwargs() -> None:
    gp_adapter_module = importlib.import_module("analytics_toolkit.sql.backends.gp.adapter")
    assert gp_adapter_module._build_gp_partition_by_sql(None) == ""
    assert gp_adapter_module._build_gp_partition_by_sql("event_date") == (
        ' PARTITION BY RANGE ("event_date")'
    )
    spec = gp_partitions_module.normalize_gp_partitions(
        RANGE_PARTITIONS,
        partition_by="event_date",
    )
    assert (
        gp_partitions_module.normalize_gp_partitions(
            spec,
            partition_by="event_date",
        )
        is spec
    )
    kwargs = get_backend_adapter("gp").build_create_from_sql_target_create_kwargs(
        gp_distributed_by_key=None,
        gp_partitions=spec,
        partition_by="event_date",
        order_by=None,
        ch_engine="ReplicatedMergeTree",
        ch_cluster="{cluster}",
        ch_sharding_key="rand()",
        ch_only_shard=False,
        drop_target_if_exists=False,
        target_exists_before_drop=False,
    )
    assert kwargs["gp_partitions"] is spec


def test_stage_target_request_propagates_initial_gp_partitions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        ddl_api,
        "_create_sql_table_with_connection",
        lambda *args, **kwargs: captured.update(kwargs),
    )
    spec = gp_partitions_module.normalize_gp_partitions(
        RANGE_PARTITIONS,
        partition_by="event_date",
    )
    get_backend_adapter("gp").ensure_stage_target_table(
        StageTargetTableRequest(
            connection=object(),
            target_table="sandbox.events",
            sample_batch=pd.DataFrame(columns=["event_date"]),
            target_column_types={"event_date": "DATE"},
            gp_distributed_by_key=None,
            partition_by="event_date",
            order_by=None,
            ch_engine="ReplicatedMergeTree",
            ch_cluster="{cluster}",
            ch_sharding_key="rand()",
            query_label=None,
            connection_key="gp",
            gp_partitions=spec,
        )
    )
    assert captured["gp_partitions"] is spec


class _RecordingCursor:
    def __init__(self, connection: _RecordingConnection) -> None:
        self.connection = connection

    def execute(self, statement: str, params: Any = None) -> None:
        del params
        self.connection.executed.append(statement)

    def close(self) -> None:
        return None


class _RecordingConnection:
    def __init__(self) -> None:
        self.executed: list[str] = []

    def cursor(self) -> _RecordingCursor:
        return _RecordingCursor(self)

    def commit(self) -> None:
        return None

    def close(self) -> None:
        return None
