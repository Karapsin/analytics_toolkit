"""External native events at real replacement cutover and metadata boundaries."""

from __future__ import annotations

from copy import deepcopy
from uuid import UUID

from analytics_toolkit.sql.backends.ch.outage_statements import exchange_or_rename
from sqlglot import exp

from tests.sql._support.assertions import strip_query_label


def cutover_commands(state):
    return [
        exchange_or_rename(strip_query_label(statement))[0]
        for statement in state.commands
        if exchange_or_rename(strip_query_label(statement)) is not None
    ]


def retarget_after_physical_swap(monkeypatch, state):
    rename = state._rename
    observed = {}

    def apply(statement, hosts, *, exchange):
        result = rename(statement, hosts, exchange=exchange)
        operation = exchange_or_rename(statement)[0]
        if (
            observed
            or not exchange
            or "events_shard" not in {table.name for table in operation.expressions}
        ):
            return result
        assert tuple(hosts) == (state.remote,)
        name = "analytics.events_shard"
        replaced = state.host_tables[state.remote].pop(name)
        create = replaced.create.copy()
        identity = UUID("00000000-0000-0000-0000-00000000abcd")
        uuid = create.find(exp.UuidProperty)
        if uuid is None:
            create.args["properties"].append(
                "expressions", exp.UuidProperty(this=exp.Literal.string(str(identity)))
            )
        else:
            uuid.set("this", exp.Literal.string(str(identity)))
        rows = [(value,) for value in range(6001, 6101)]
        state._create(state.remote, create, rows)
        observed.update(
            uuid=identity,
            rows=rows,
            acknowledged_rows=deepcopy(replaced.rows),
            before_facade=deepcopy(state.snapshot()),
        )
        return result

    monkeypatch.setattr(state, "_rename", apply)
    return observed


class NativeCatalogPermissionError(PermissionError):
    code = 497


def fail_before_first_cutover_wire(state, failure):
    observed = {"proofs": 0, "raised": False}

    def metadata(tree, _rows):
        if not any(
            name.startswith("analytics.events_shard__replace_guarded_") and len(table.rows) == 2
            for name, table in state.host_tables[state.remote].items()
        ):
            return
        if not any(projection.alias_or_name == "uuid" for projection in tree.expressions):
            return
        literals = {literal.this for literal in tree.find_all(exp.Literal) if literal.is_string}
        if "events_shard" not in literals:
            return
        observed["proofs"] += 1
        # The original identity recheck and initial step recheck precede the
        # outage client's final native preflight; no driver submission yet.
        if observed["proofs"] == 3:
            assert cutover_commands(state) == []
            observed["raised"] = True
            raise failure
        return

    state.metadata_transform = metadata
    return observed
