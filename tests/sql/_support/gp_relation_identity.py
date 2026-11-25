"""Model actual GP relation generations in owned native checkpoint fixtures."""

from __future__ import annotations

from analytics_toolkit.sql.connection.config import get_connection_config
from sqlglot import parse_one

from tests.sql._support.created_gp_relations import locked_relation


def relation_identity_metadata(self, statement, params):
    if all(
        fragment in statement
        for fragment in ("pg_catalog.pg_class", "pg_catalog.to_regclass", "c.relfilenode")
    ):
        assert len(params) == 1
        node = parse_one(f"SELECT * FROM {params[0]}", read="postgres").args["from_"].this
        identity = self.identity(node)
        table = self.tables.get(identity)
        if table is None:
            return []
        if not hasattr(table, "native_creation_oid"):
            self._next_relation_oid = getattr(self, "_next_relation_oid", 10000) + 1
            table.native_creation_oid = self._next_relation_oid
        namespaces = self.__dict__.setdefault("_native_namespace_oids", {})
        namespace = namespaces.setdefault(identity[0], 2000 + len(namespaces))
        oid = table.native_creation_oid
        return [
            (
                get_connection_config("gp").database,
                "127.0.0.1",
                5432,
                oid,
                oid + 100000,
                namespace,
            )
        ]
    locked = locked_relation(statement)
    if locked is not None:
        assert self.identity(locked) in self.tables
        return []
    return None


class RelationIdentityDatabase:
    def metadata(self, statement, params):
        result = relation_identity_metadata(self, statement, params)
        return super().metadata(statement, params) if result is None else result
