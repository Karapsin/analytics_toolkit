"""Drop metadata includes native views without inventing an ENGINE clause."""

from __future__ import annotations

from analytics_toolkit.sql.backends.ch.reconfigure_ddl import engine_sql
from sqlglot import exp

from tests.sql._support.reconfigure_conversion_scope import ConversionScopeClient


def view_metadata(monkeypatch, state):
    ordinary = state._metadata

    def records(name, hosts):
        if name not in {"system.tables", "system.columns"}:
            return ordinary(name, hosts)
        result = ConversionScopeClient._metadata(state, name, hosts)
        for record in result:
            host = next(
                host for host in hosts if state.server_identities[host][0] == record["serverUUID"]
            )
            relation = record["table"] if name == "system.columns" else record["name"]
            value = state.host_tables[host][f"{record['database']}.{relation}"]
            record["engine_full"] = (
                engine_sql(value.create) if value.create.find(exp.EngineProperty) else value.engine
            )
            record["uuid"] = value.uuid
        return result

    monkeypatch.setattr(state, "_metadata", records)
