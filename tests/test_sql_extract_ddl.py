from __future__ import annotations

import importlib

import pandas as pd
import pytest

from analytics_toolkit.sql.connection.errors import InvalidSqlInputError


extract_ddl_module = importlib.import_module("analytics_toolkit.sql.ddl.extract_ddl")
gp_ddl_module = importlib.import_module("analytics_toolkit.sql.backends.gp.ddl")
ddl_module = importlib.import_module("analytics_toolkit.sql.ddl")
sql_module = importlib.import_module("analytics_toolkit.sql")


def _capture_read_sql(
    monkeypatch: pytest.MonkeyPatch,
    ddl_for_query: dict[str, str] | None = None,
) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []

    def fake_read_sql(connection_type: str, query: str) -> pd.DataFrame:
        calls.append((connection_type, query))
        ddl = (
            ddl_for_query[query]
            if ddl_for_query is not None
            else "CREATE TABLE analytics.events (id UInt64)"
        )
        return pd.DataFrame({"ddl": [ddl]})

    monkeypatch.setattr(extract_ddl_module, "read_sql", fake_read_sql)
    return calls


def test_extract_ddl_is_public_api() -> None:
    assert sql_module.extract_ddl is extract_ddl_module.extract_ddl
    assert ddl_module.extract_ddl is extract_ddl_module.extract_ddl
    assert "extract_ddl" in sql_module.__all__
    assert "extract_ddl" in ddl_module.__all__
    assert "extract_ddl" in sql_module._TIMED_PUBLIC_SQL_FUNCTION_NAMES
    assert getattr(sql_module.extract_ddl, "__sql_public_timing__", False)


def test_extract_ddl_clickhouse_single_table_returns_semicolon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture_read_sql(
        monkeypatch,
        {
            "SHOW CREATE TABLE analytics.events": (
                "CREATE TABLE analytics.events (id UInt64)"
            ),
        },
    )

    result = extract_ddl_module.extract_ddl("ch", "analytics.events")

    assert calls == [("ch", "SHOW CREATE TABLE analytics.events")]
    assert result == "CREATE TABLE analytics.events (id UInt64);"


def test_extract_ddl_preserves_table_order_and_normalizes_semicolons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture_read_sql(
        monkeypatch,
        {
            "SHOW CREATE TABLE mart.orders": " CREATE TABLE mart.orders (id UInt64); ",
            "SHOW CREATE TABLE mart.users": "\nCREATE TABLE mart.users (id UInt64)\n",
        },
    )

    result = extract_ddl_module.extract_ddl(
        "ch",
        ["mart.orders", "mart.users"],
    )

    assert calls == [
        ("ch", "SHOW CREATE TABLE mart.orders"),
        ("ch", "SHOW CREATE TABLE mart.users"),
    ]
    assert result == (
        "CREATE TABLE mart.orders (id UInt64);\n"
        "CREATE TABLE mart.users (id UInt64);"
    )


def test_extract_ddl_trino_resolves_unqualified_table_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture_read_sql(
        monkeypatch,
        {
            "SHOW CREATE TABLE iceberg.sandbox.events": (
                "CREATE TABLE iceberg.sandbox.events (id bigint)"
            ),
        },
    )

    result = extract_ddl_module.extract_ddl("trino", "events")

    assert calls == [("trino", "SHOW CREATE TABLE iceberg.sandbox.events")]
    assert result == "CREATE TABLE iceberg.sandbox.events (id bigint);"


def test_extract_ddl_trino_resolves_schema_qualified_table_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture_read_sql(
        monkeypatch,
        {
            "SHOW CREATE TABLE iceberg.mart.events": (
                "CREATE TABLE iceberg.mart.events (id bigint)"
            ),
        },
    )

    result = extract_ddl_module.extract_ddl("trino", "mart.events")

    assert calls == [("trino", "SHOW CREATE TABLE iceberg.mart.events")]
    assert result == "CREATE TABLE iceberg.mart.events (id bigint);"


def test_extract_ddl_greenplum_uses_pg_get_tabledef(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture_read_sql(
        monkeypatch,
        {
            (
                "SELECT "
                "pg_catalog.pg_get_tabledef(pg_catalog.to_regclass('mart.orders')::oid) "
                "AS ddl"
            ): "CREATE TABLE mart.orders (id bigint)",
        },
    )

    result = extract_ddl_module.extract_ddl("gp", "mart.orders")

    assert calls == [
        (
            "gp",
            "SELECT "
            "pg_catalog.pg_get_tabledef(pg_catalog.to_regclass('mart.orders')::oid) "
            "AS ddl",
        )
    ]
    assert result == "CREATE TABLE mart.orders (id bigint);"


def test_extract_ddl_greenplum_escapes_table_name_literal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture_read_sql(
        monkeypatch,
        {
            (
                "SELECT "
                "pg_catalog.pg_get_tabledef(pg_catalog.to_regclass('mart.o''rders')::oid) "
                "AS ddl"
            ): "CREATE TABLE mart.\"o'rders\" (id bigint)",
        },
    )

    result = extract_ddl_module.extract_ddl("gp", "mart.o'rders")

    assert calls == [
        (
            "gp",
            "SELECT "
            "pg_catalog.pg_get_tabledef(pg_catalog.to_regclass('mart.o''rders')::oid) "
            "AS ddl",
        )
    ]
    assert result == "CREATE TABLE mart.\"o'rders\" (id bigint);"


def test_extract_ddl_greenplum_falls_back_to_catalog_ddl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    def fake_read_sql(connection_type: str, query: str) -> pd.DataFrame:
        calls.append((connection_type, query))
        if "pg_get_tabledef" in query:
            raise UndefinedFunction(
                "function pg_catalog.pg_get_tabledef(oid) does not exist",
            )
        if "FROM pg_catalog.pg_class AS c" in query:
            return pd.DataFrame(
                {
                    "oid": ["12345"],
                    "schema_name": ["mart"],
                    "relation_name": ["orders"],
                    "relkind": ["r"],
                    "reloptions": [["appendonly=true", "orientation=column"]],
                    "table_comment": ["Orders table"],
                },
            )
        if "FROM pg_catalog.pg_attribute AS a" in query:
            return pd.DataFrame(
                {
                    "attnum": [1, 2],
                    "column_name": ["id", "payload"],
                    "formatted_type": ["bigint", "text"],
                    "default_expr": ["nextval('orders_id_seq'::regclass)", None],
                    "is_not_null": [True, False],
                    "column_comment": ["Order id", None],
                },
            )
        if "FROM pg_catalog.pg_inherits AS i" in query:
            return pd.DataFrame(columns=["parent_schema", "parent_table"])
        if "FROM pg_catalog.pg_index AS i" in query:
            return pd.DataFrame(
                {
                    "index_name": ["orders_payload_idx"],
                    "index_def": [
                        "CREATE INDEX orders_payload_idx "
                        "ON mart.orders USING btree (payload)",
                    ],
                },
            )
        if "FROM pg_catalog.pg_constraint" in query:
            return pd.DataFrame(
                {
                    "constraint_name": ["orders_pkey"],
                    "constraint_type": ["p"],
                    "constraint_def": ["PRIMARY KEY (id)"],
                },
            )
        if "FROM pg_catalog.pg_proc AS p" in query:
            return pd.DataFrame(
                {"has_partkeydef": [True], "has_partition_def": [False]},
            )
        if "pg_get_partkeydef" in query:
            return pd.DataFrame({"partition_def": ["RANGE (id)"]})
        if "FROM gp_distribution_policy" in query:
            return pd.DataFrame({"policy_type": ["p"], "attrnums": ["{1}"]})
        raise AssertionError(f"Unexpected query: {query}")

    monkeypatch.setattr(extract_ddl_module, "read_sql", fake_read_sql)

    result = extract_ddl_module.extract_ddl("gp", "mart.orders")

    assert [connection_key for connection_key, _ in calls] == ["gp"] * len(calls)
    assert result == (
        'CREATE TABLE "mart"."orders" (\n'
        '    "id" bigint DEFAULT nextval(\'orders_id_seq\'::regclass) NOT NULL,\n'
        '    "payload" text,\n'
        '    CONSTRAINT "orders_pkey" PRIMARY KEY (id)\n'
        ")\n"
        "WITH (\n"
        "    appendonly=true,\n"
        "    orientation=column\n"
        ")\n"
        "PARTITION BY RANGE (id)\n"
        'DISTRIBUTED BY ("id");\n'
        "CREATE INDEX orders_payload_idx ON mart.orders USING btree (payload);\n"
        'COMMENT ON TABLE "mart"."orders" IS \'Orders table\';\n'
        'COMMENT ON COLUMN "mart"."orders"."id" IS \'Order id\';'
    )


def test_extract_ddl_greenplum_reraises_unrelated_undefined_function(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_read_sql(connection_type: str, query: str) -> pd.DataFrame:
        del connection_type, query
        raise UndefinedFunction("function other_helper() does not exist")

    monkeypatch.setattr(extract_ddl_module, "read_sql", fake_read_sql)

    with pytest.raises(UndefinedFunction, match="other_helper"):
        extract_ddl_module.extract_ddl("gp", "mart.orders")


def test_extract_ddl_greenplum_fallback_raises_when_table_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_read_sql(connection_type: str, query: str) -> pd.DataFrame:
        del connection_type
        if "pg_get_tabledef" in query:
            raise UndefinedFunction(
                "function pg_catalog.pg_get_tabledef(oid) does not exist",
            )
        return pd.DataFrame()

    monkeypatch.setattr(extract_ddl_module, "read_sql", fake_read_sql)

    with pytest.raises(ValueError, match="No metadata returned for table mart.orders"):
        extract_ddl_module.extract_ddl("gp", "mart.orders")


@pytest.mark.parametrize(
    "result",
    [
        pd.DataFrame(),
        pd.DataFrame(index=[0]),
        pd.DataFrame({"ddl": [None]}),
    ],
)
def test_first_result_value_rejects_missing_ddl(result: pd.DataFrame) -> None:
    with pytest.raises(ValueError, match="No DDL returned"):
        extract_ddl_module._first_result_value(result, "mart.orders")


def test_first_result_value_stringifies_present_ddl() -> None:
    result = pd.DataFrame({"ddl": [123]})

    assert extract_ddl_module._first_result_value(result, "mart.orders") == "123"


@pytest.mark.parametrize(
    ("policy_type", "attrnums", "expected"),
    [
        ("p", "{1,2}", 'DISTRIBUTED BY ("id", "payload")'),
        ("p", "{}", "DISTRIBUTED RANDOMLY"),
        ("r", "", "DISTRIBUTED REPLICATED"),
    ],
)
def test_extract_ddl_greenplum_formats_distribution_policies(
    policy_type: str,
    attrnums: str,
    expected: str,
) -> None:
    columns = pd.DataFrame(
        {
            "attnum": [1, 2],
            "column_name": ["id", "payload"],
        },
    )
    policy = pd.DataFrame({"policy_type": [policy_type], "attrnums": [attrnums]})

    assert (
        gp_ddl_module.format_gp_distribution_clause(policy, columns)
        == expected
    )


@pytest.mark.parametrize(
    ("tables", "error_type", "match"),
    [
        ([], InvalidSqlInputError, "tables must not be empty"),
        (" ", InvalidSqlInputError, "tables values must not be empty"),
        (["orders", " "], InvalidSqlInputError, "tables values must not be empty"),
        (b"orders", TypeError, "tables must be a string or a sequence of strings"),
        (["orders", 1], TypeError, "tables values must be strings"),
    ],
)
def test_extract_ddl_rejects_invalid_table_inputs(
    monkeypatch: pytest.MonkeyPatch,
    tables: object,
    error_type: type[Exception],
    match: str,
) -> None:
    monkeypatch.setattr(
        extract_ddl_module,
        "read_sql",
        _fail_read_sql,
    )

    with pytest.raises(error_type, match=match):
        extract_ddl_module.extract_ddl("ch", tables)


def test_extract_ddl_raises_when_backend_returns_no_ddl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_read_sql(connection_type: str, query: str) -> pd.DataFrame:
        del connection_type, query
        return pd.DataFrame({"ddl": [None]})

    monkeypatch.setattr(extract_ddl_module, "read_sql", fake_read_sql)

    with pytest.raises(ValueError, match="No DDL returned for table mart.orders"):
        extract_ddl_module.extract_ddl("ch", "mart.orders")


def _fail_read_sql(_connection_type: str, _query: str) -> pd.DataFrame:
    raise AssertionError("read_sql should not be called")


class UndefinedFunction(Exception):
    pgcode = "42883"
