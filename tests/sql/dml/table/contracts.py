from __future__ import annotations

from tests.sql._support.cross_area import (
    identifiers_module,
    plans_module,
    pytest,
    sql_module,
)


def test_table_identifier_preserves_qualified_parts_and_quotes() -> None:
    identifier = identifiers_module.parse_table_identifier(
        'sandbox."Target Table"',
        "gp",
    )

    assert identifier.parts == ("sandbox", "Target Table")
    assert identifier.with_relation_suffix("_stage").render("gp") == (
        'sandbox."Target Table_stage"'
    )
    assert identifier.render_quoted("ch") == "`sandbox`.`Target Table`"


@pytest.mark.parametrize(
    ("backend", "table_name"),
    [
        ("gp", "sandbox.events"),
        ("gp", '"sandbox"."Target Table"'),
        ("trino", "catalog.schema.events"),
        ("trino", '"catalog"."schema"."Target Table"'),
        ("ch", "sandbox.events"),
        ("ch", "`sandbox`.`Target Table`"),
    ],
)
def test_table_identifier_round_trips_rendered_parts(
    backend: str,
    table_name: str,
) -> None:
    identifier = identifiers_module.parse_table_identifier(table_name, backend)
    rendered = identifier.render(backend)
    reparsed = identifiers_module.parse_table_identifier(rendered, backend)

    assert reparsed.parts == identifier.parts
    assert reparsed.quoted == identifier.quoted


def test_drop_tables_dry_run_public_timing_uses_optional_time_print_kwargs(
    capsys,
) -> None:
    plan = sql_module.drop_tables(
        "ch",
        "sandbox.events",
        dry_run=True,
    )

    output = capsys.readouterr().out
    assert isinstance(plan, plans_module.SqlPlan)
    assert plan.operation == "drop_tables"
    assert "[drop_tables] [timing] Finished SQL function in " in output
