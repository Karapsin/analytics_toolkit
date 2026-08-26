from __future__ import annotations

from tests.sql_format._support.formatting import (
    exp,
    sql_format,
)


def test_sqlglot_with_and_from_argument_name_compatibility(monkeypatch) -> None:
    current_arg_types = exp.Select.arg_types
    legacy_arg_types = {
        key: value for key, value in current_arg_types.items() if key not in {"with_", "from_"}
    }
    legacy_arg_types.update({"with": False, "from": False})
    monkeypatch.setattr(exp.Select, "arg_types", legacy_arg_types)

    assert sql_format._with_arg_name() == "with"
    assert sql_format._from_arg_name() == "from"

    select = exp.Select(expressions=[exp.Star()])
    select.set("from", exp.From(this=exp.to_table("tmp")))
    assert sql_format._is_temp_reference_select(select, temp_names={"tmp"})
