from __future__ import annotations

# ruff: noqa: E501
import math

import pytest
from analytics_toolkit.sql.backends.ch.creation_policy import (
    build_policy_create_sqls,
    resolve_clickhouse_creation_policy,
    validate_distributed_template,
)
from analytics_toolkit.sql.backends.trino.parquet_stage import build_parquet_stage_table_sql
from analytics_toolkit.sql.connection.ddl_defaults import (
    legacy_clickhouse_scope,
    parse_ddl_defaults,
)
from analytics_toolkit.sql.connection.errors import SqlConfigError
from analytics_toolkit.sql.ddl.api import create_sql_table
from analytics_toolkit.sql.ddl.properties import (
    overlay_with_properties,
    render_ddl_property_value,
)
from analytics_toolkit.sql.dml.transfer.flow.finalize import _stage_tables_to_cleanup
from analytics_toolkit.sql.dml.transfer.runtime.models import TransferStageState


def test_property_defaults_normalize_and_render_supported_values() -> None:
    defaults = parse_ddl_defaults(
        {
            "regular": {
                "Flag": True,
                "count": 3,
                "ratio": 1.5,
                "expr": " ARRAY['x'] ",
                "items": ["a'b", 2, False],
            }
        },
        "target",
        "trino",
    )
    sql = overlay_with_properties(
        "CREATE TABLE x (id bigint) WITH (format = 'PARQUET', old = true)",
        {**defaults.regular, "old": None},
    )
    assert "flag = true" in sql
    assert "count = 3" in sql
    assert "ratio = 1.5" in sql
    assert "expr = ARRAY['x']" in sql
    assert "items = ARRAY['a''b', 2, false]" in sql
    assert "old" not in sql


@pytest.mark.parametrize(
    "value",
    ["", {}, math.inf, -math.inf, math.nan],
)
def test_property_defaults_reject_unsupported_values(value: object) -> None:
    with pytest.raises(SqlConfigError, match="unsupported property value"):
        parse_ddl_defaults({"regular": {"value": value}}, "target", "gp")


def test_property_defaults_reject_duplicate_normalized_keys() -> None:
    with pytest.raises(SqlConfigError, match="duplicate property 'format'"):
        parse_ddl_defaults(
            {"regular": {"format": "'PARQUET'", "FORMAT": "'ORC'"}},
            "target",
            "trino",
        )


@pytest.mark.parametrize(
    ("raw", "backend", "message"),
    [
        ([], "gp", "ddl_defaults.*JSON object"),
        ({"regular": []}, "gp", "regular.*JSON object"),
        ({"regular": {"bad-key": True}}, "gp", "invalid SQL property key"),
        ({"regular": {"items": [object()]}}, "gp", "unsupported property value"),
        ({"regular": []}, "ch", "regular.*JSON object"),
        ({"regular": {"unknown": True}}, "ch", "unsupported ClickHouse field"),
        (
            {"regular": {"create_distributed_pair": 1}},
            "ch",
            "create_distributed_pair must be a boolean",
        ),
        ({"regular": {"shard": []}}, "ch", "shard must be a JSON object"),
        ({"regular": {"shard": {"unknown": "x"}}}, "ch", "unsupported field"),
        (
            {"regular": {"distributed": []}},
            "ch",
            "distributed must be a JSON object",
        ),
        (
            {"regular": {"distributed": {"unknown": "x"}}},
            "ch",
            "unsupported field",
        ),
        (
            {"regular": {"shard": {"engine": None}}},
            "ch",
            "engine must be a non-empty string",
        ),
        (
            {"regular": {"distributed": {"on_cluster": " "}}},
            "ch",
            "on_cluster must be a non-empty string or null",
        ),
    ],
)
def test_ddl_defaults_reject_malformed_structures(raw: object, backend: str, message: str) -> None:
    with pytest.raises(SqlConfigError, match=message):
        parse_ddl_defaults(raw, "target", backend)


def test_property_renderer_edge_cases() -> None:
    assert render_ddl_property_value([None, "x"]) == "ARRAY[NULL, 'x']"
    with pytest.raises(TypeError, match="Unsupported DDL property value"):
        render_ddl_property_value(object())
    sql = "CREATE TABLE x (id bigint) WITH (flag, nested = ARRAY[1, 2], call = fn('a,b'))"
    assert overlay_with_properties(sql, {}) == sql
    rendered = overlay_with_properties(sql, {"added": True})
    assert "nested = ARRAY[1, 2]" in rendered
    assert "call = fn('a,b')" in rendered
    assert "added = true" in rendered
    malformed = "CREATE TABLE x (id bigint) WITH (nested = fn(1)"
    assert overlay_with_properties(malformed, {"added": True}) == malformed


def test_parquet_stage_properties_restore_workflow_required_values() -> None:
    class Adapter:
        @staticmethod
        def quote_identifier(value: str) -> str:
            return f'"{value}"'

    sql = build_parquet_stage_table_sql(
        Adapter(),
        "catalog.schema.stage",
        {"id": "bigint"},
        "s3://bucket/generated",
        ddl_properties={
            "format": "'ORC'",
            "external_location": "'s3://wrong'",
            "partitioning": ["day"],
        },
    )
    assert "format = 'PARQUET'" in sql
    assert "external_location = 's3://bucket/generated'" in sql
    assert "partitioning = ARRAY['day']" in sql


def test_trino_explicit_properties_override_connection_defaults() -> None:
    sql = create_sql_table(
        "trino",
        "memory.default.events",
        table_schema={"id": "bigint"},
        partition_by="explicit_day",
        order_by="explicit_id",
        only_generate_sql=True,
    )
    assert isinstance(sql, str)
    assert "partitioning = ARRAY['explicit_day']" in sql
    assert "sorted_by = ARRAY['explicit_id']" in sql


def test_transfer_cleanup_handles_missing_stage_tables() -> None:
    state = TransferStageState(target_exists=False)
    assert _stage_tables_to_cleanup(state) == []


def test_backend_invalid_scope_is_rejected() -> None:
    with pytest.raises(SqlConfigError, match=r"unsupported scope.*gp"):
        parse_ddl_defaults({"parquet_staging": {}}, "target", "gp")


def test_clickhouse_policy_keeps_execution_clusters_independent() -> None:
    defaults = parse_ddl_defaults(
        {
            "regular": {
                "create_distributed_pair": True,
                "shard": {"engine": "ReplicatedMergeTree", "on_cluster": "CORE"},
                "distributed": {
                    "engine_template": "Distributed('old', 'old_db', 'old_table', rand(), 'policy')",
                    "cluster": "routing",
                    "on_cluster": "{cluster}",
                    "sharding_key": "rand()",
                },
            }
        },
        "target",
        "ch",
    )
    policy = resolve_clickhouse_creation_policy(
        defaults.regular,
        ch_engine=None,
        ch_cluster=None,
        ch_sharding_key="cityHash64(user_id)",
        ch_distributed_table=None,
        ch_only_shard=False,
        ch_distributed_engine_template=None,
        ch_distributed_cluster="route_override",
        ch_shard_on_cluster=None,
        ch_distributed_on_cluster=None,
    )
    sqls = build_policy_create_sqls(
        table_name="analytics.events",
        joined_columns="user_id UInt64",
        partition_by=None,
        order_by=None,
        policy=policy,
        ch_only_shard=False,
        ch_replace_table=False,
    )
    assert "ON CLUSTER CORE" in sqls[0]
    assert "ON CLUSTER '{cluster}'" in sqls[2]
    assert "'route_override'," in sqls[2]
    assert "'analytics'," in sqls[2]
    assert "'events_shard'," in sqls[2]
    assert "cityHash64(user_id)," in sqls[2]
    assert "'policy'" in sqls[2]


@pytest.mark.parametrize(
    "template",
    [
        "MergeTree()",
        "Distributed({unknown}, x, y)",
        "Distributed({cluster!r}, x, y)",
        "Distributed({cluster",
    ],
)
def test_clickhouse_template_validation(template: str) -> None:
    with pytest.raises(SqlConfigError):
        validate_distributed_template(template)


def test_clickhouse_template_validation_rejects_invalid_sql() -> None:
    with pytest.raises(SqlConfigError, match="valid SQL expression"):
        validate_distributed_template("Distributed('x', 'y', 'z'")


@pytest.mark.parametrize(
    ("scope", "message"),
    [
        ({}, "create_distributed_pair"),
        ({"create_distributed_pair": False}, "shard.engine"),
        (
            {"create_distributed_pair": False, "shard": {"engine": "MergeTree"}},
            "shard.on_cluster",
        ),
        (
            {
                "create_distributed_pair": True,
                "shard": {"engine": "MergeTree", "on_cluster": None},
            },
            "distributed.engine_template",
        ),
        (
            {
                "create_distributed_pair": True,
                "shard": {"engine": "MergeTree", "on_cluster": None},
                "distributed": {"engine_template": "Distributed('x', x, y)"},
            },
            "distributed.on_cluster",
        ),
    ],
)
def test_clickhouse_policy_reports_missing_required_fields(
    scope: dict[str, object], message: str
) -> None:
    defaults = parse_ddl_defaults({"regular": scope}, "target", "ch")
    with pytest.raises(SqlConfigError, match=message):
        resolve_clickhouse_creation_policy(
            defaults.regular,
            ch_engine=None,
            ch_cluster=None,
            ch_sharding_key=None,
            ch_distributed_table=None,
            ch_only_shard=False,
            ch_distributed_engine_template=None,
            ch_distributed_cluster=None,
        )


def test_clickhouse_dedicated_clusters_and_three_argument_template() -> None:
    defaults = parse_ddl_defaults(
        {
            "regular": {
                "create_distributed_pair": True,
                "shard": {"engine": "MergeTree", "on_cluster": "configured"},
                "distributed": {
                    "engine_template": "Distributed('hardcoded', 'old', 'old_table')",
                    "on_cluster": None,
                },
            }
        },
        "target",
        "ch",
    )
    policy = resolve_clickhouse_creation_policy(
        defaults.regular,
        ch_engine=None,
        ch_cluster="legacy",
        ch_sharding_key="cityHash64(id)",
        ch_distributed_table=None,
        ch_only_shard=False,
        ch_distributed_engine_template=None,
        ch_distributed_cluster=None,
        ch_shard_on_cluster="dedicated",
        ch_distributed_on_cluster=None,
        warn_ch_cluster=False,
    )
    sqls = build_policy_create_sqls(
        table_name="events",
        joined_columns="id UInt64",
        partition_by=None,
        order_by=None,
        policy=policy,
        ch_only_shard=False,
        ch_replace_table=True,
    )
    assert "ON CLUSTER dedicated" in sqls[0]
    assert "ON CLUSTER legacy" in sqls[2]
    assert "cityHash64(id)" in sqls[2]


def test_clickhouse_hardcoded_template_arguments_are_preserved_when_unset() -> None:
    defaults = parse_ddl_defaults(
        {
            "regular": {
                "create_distributed_pair": True,
                "shard": {"engine": "MergeTree", "on_cluster": None},
                "distributed": {
                    "engine_template": "Distributed('hardcoded', 'old', 'old_table', rand())",
                    "on_cluster": None,
                },
            }
        },
        "target",
        "ch",
    )
    policy = resolve_clickhouse_creation_policy(
        defaults.regular,
        ch_engine=None,
        ch_cluster=None,
        ch_sharding_key=None,
        ch_distributed_table=None,
        ch_only_shard=False,
        ch_distributed_engine_template=None,
        ch_distributed_cluster=None,
        ch_shard_on_cluster=None,
        ch_distributed_on_cluster=None,
    )
    sqls = build_policy_create_sqls(
        table_name="events",
        joined_columns="id UInt64",
        partition_by=None,
        order_by=None,
        policy=policy,
        ch_only_shard=False,
        ch_replace_table=False,
    )
    assert len(sqls) == 2
    assert "'hardcoded'" in sqls[1]
    assert "randCanonical()" in sqls[1]


def test_legacy_clickhouse_staging_scope_is_complete() -> None:
    scope = legacy_clickhouse_scope(staging=True)
    assert scope.create_distributed_pair is False
    assert scope.shard.engine == "MergeTree"
    assert scope.shard.on_cluster is None
    regular = legacy_clickhouse_scope()
    assert regular.create_distributed_pair is True
    assert regular.distributed.cluster == "{cluster}"


def test_clickhouse_only_shard_does_not_require_distributed_pair_default() -> None:
    defaults = parse_ddl_defaults({}, "target", "ch")
    policy = resolve_clickhouse_creation_policy(
        defaults.regular,
        ch_engine="MergeTree",
        ch_cluster="integration_cluster",
        ch_sharding_key=None,
        ch_distributed_table=None,
        ch_only_shard=True,
        ch_distributed_engine_template=None,
        ch_distributed_cluster=None,
        ch_shard_on_cluster=None,
        ch_distributed_on_cluster=None,
        warn_ch_cluster=False,
    )
    assert policy.create_distributed_pair is False
    assert policy.shard_on_cluster == "integration_cluster"


def test_clickhouse_null_on_cluster_omits_cluster_and_local_duplicate() -> None:
    defaults = parse_ddl_defaults(
        {
            "staging": {
                "create_distributed_pair": False,
                "shard": {"engine": "MergeTree", "on_cluster": None},
            }
        },
        "target",
        "ch",
    )
    policy = resolve_clickhouse_creation_policy(
        defaults.staging,
        ch_engine=None,
        ch_cluster=None,
        ch_sharding_key=None,
        ch_distributed_table=None,
        ch_only_shard=False,
        ch_distributed_engine_template=None,
        ch_distributed_cluster=None,
        ch_shard_on_cluster=None,
        ch_distributed_on_cluster=None,
    )
    sqls = build_policy_create_sqls(
        table_name="analytics.stage",
        joined_columns="id UInt64",
        partition_by=None,
        order_by=None,
        policy=policy,
        ch_only_shard=False,
        ch_replace_table=False,
    )
    assert len(sqls) == 1
    assert "ON CLUSTER" not in sqls[0]
    assert "analytics.stage_shard" not in sqls[0]


def test_public_ch_cluster_compatibility_argument_warns() -> None:
    with pytest.warns(DeprecationWarning, match="ch_cluster is deprecated"):
        create_sql_table(
            "ch",
            "analytics.events",
            table_schema={"id": "UInt64"},
            ch_cluster="legacy",
            only_generate_sql=True,
        )
