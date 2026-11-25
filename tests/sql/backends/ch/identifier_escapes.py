"""Raw ClickHouse metadata identifiers survive SQL generation and reparsing."""

from __future__ import annotations

import importlib

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.ddl import build_ch_shard_table_name
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.core.identifiers import TableIdentifier
from analytics_toolkit.sql_explorer.identifier_completion import quote_completion_name
from sqlglot import exp, parse_one

_NAMES = [r"path\next", "tail\\", r"path\`part", r"two\\slashes"]
creation_module = importlib.import_module("analytics_toolkit.sql.ddl.api")


@pytest.mark.parametrize("name", _NAMES)
@pytest.mark.parametrize("source", ["schema", "dataframe"])
@pytest.mark.parametrize("mode", ["only_generate_sql", "return_sql"])
def test_public_clickhouse_creation_preserves_raw_backslashes_in_column_names(
    monkeypatch: pytest.MonkeyPatch, name: str, source: str, mode: str
) -> None:
    def unexpected_connection(_key: str) -> None:
        pytest.fail("Pure identifier planning opened a database connection.")

    monkeypatch.setattr(creation_module, "get_sql_connection", unexpected_connection)
    options = (
        {"table_schema": {name: "Int64"}}
        if source == "schema"
        else {"df": pd.DataFrame({name: [7]})}
    )
    result = sql.create_table("ch", "sandbox.events", ch_only_shard=True, **options, **{mode: True})
    statements = [result] if isinstance(result, str) else result.sqls

    assert statements
    for statement in statements:
        tree = parse_one(statement, read=ClickHouseDialect)
        creates = list(tree.find_all(exp.Create))
        assert creates
        for create in creates:
            assert [column.name for column in create.find_all(exp.ColumnDef)] == [name]


@pytest.mark.parametrize("name", _NAMES)
def test_clickhouse_adapter_projection_and_structured_table_parts_keep_exact_names(
    name: str,
) -> None:
    adapter = get_backend_adapter("ch")
    projection = adapter.cast_select_expression(name, "Int64")
    tree = parse_one(f"SELECT {projection} FROM events", read=ClickHouseDialect)
    assert tree.expressions[0].alias == name
    assert tree.expressions[0].find(exp.Column).name == name

    reference = TableIdentifier((name, name + ".events"), (True, True))
    parsed = TableIdentifier.parse(reference.render_quoted("ch"), "ch")
    assert parsed.parts == reference.parts
    assert parsed.quoted == (True, True)
    shard = TableIdentifier.parse(build_ch_shard_table_name(reference.render_quoted("ch")), "ch")
    assert shard.parts == (name, name + ".events_shard")
    assert shard.quoted == (True, True)


@pytest.mark.parametrize("name", _NAMES)
def test_clickhouse_shared_ast_and_completion_rendering_keep_exact_identifier_bytes(
    name: str,
) -> None:
    expression = exp.Column(this=exp.to_identifier(name, quoted=True))
    rendered = expression.sql(dialect=ClickHouseDialect)
    assert parse_one(rendered, read=ClickHouseDialect).name == name
    completion = quote_completion_name(name, "ch")
    assert parse_one(completion, read=ClickHouseDialect).name == name
    assert quote_completion_name("events", "ch") == "events"
    reserved = parse_one(quote_completion_name("select", "ch"), read=ClickHouseDialect)
    assert reserved.name == "select"


@pytest.mark.parametrize("backend", ["gp", "trino"])
@pytest.mark.parametrize("name", _NAMES)
def test_other_backends_keep_their_existing_backslash_identifier_semantics(
    backend: str, name: str
) -> None:
    adapter = get_backend_adapter(backend)
    quoted = adapter.quote_identifier(name)
    assert quoted == '"' + name.replace('"', '""') + '"'
