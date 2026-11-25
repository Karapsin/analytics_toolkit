"""Catalog-aware GP domain storage with visible built-in-name shadowing."""

from __future__ import annotations

from sqlglot import exp, parse_one

from tests.sql._support.gp_dates import _DateConnection
from tests.sql._support.gp_native_scalars import NativeScalarDatabase
from tests.sql._support.gp_numeric_text import _literal_text
from tests.sql._support.load_table import load_df_module


class ShadowDomainDatabase(NativeScalarDatabase):
    def __init__(self):
        super().__init__("BIGINT")
        self.target.schema = {"payload": "int4"}
        self.target.rows = [{"payload": 150}]
        self.type_catalog = {"namespace": "app", "name": "int4", "kind": "d"}

    @staticmethod
    def decode_value(node, native_type):
        if native_type.replace('"', "").lower() in {"int4", "app.int4"}:
            text = _literal_text(node)
            if text is None:
                return None
            value = int(text)
            if value <= 100:
                message = "Native domain CHECK constraint rejected the assigned value."
                raise ValueError(message)
            return value
        return NativeScalarDatabase.decode_value(node, native_type)

    def metadata(self, statement, params):
        tree = parse_one(statement, read="postgres")
        formatters = [
            node for node in tree.find_all(exp.Anonymous) if node.name.lower() == "format_type"
        ]
        if formatters and params == ("sandbox", "events"):
            return [("payload", self.catalog_expression(tree.expressions[1]))]
        return super().metadata(statement, params)

    def catalog_expression(self, node):
        while isinstance(node, (exp.Alias, exp.Paren, exp.Dot)):
            node = node.expression if isinstance(node, exp.Dot) else node.this
        if isinstance(node, (exp.Literal, exp.Column)):
            return self._catalog_atom(node)
        if isinstance(node, exp.Binary):
            return self._catalog_binary(node)
        if isinstance(node, exp.Greatest):
            return max(self.catalog_expression(item) for item in (node.this, *node.expressions))
        if isinstance(node, exp.Case):
            return self._catalog_case(node)
        if isinstance(node, exp.Anonymous):
            return self._catalog_function(node)
        return self._unsupported_catalog_expression(node)

    def _catalog_atom(self, node):
        if isinstance(node, exp.Literal):
            return node.this if node.is_string else int(node.this)
        return {
            "attndims": 0,
            "typtype": self.type_catalog["kind"],
            "typname": self.type_catalog["name"],
            "nspname": self.type_catalog["namespace"],
        }[node.name]

    def _catalog_binary(self, node):
        if isinstance(node, exp.DPipe):
            return str(self.catalog_expression(node.left)) + str(
                self.catalog_expression(node.right)
            )
        if isinstance(node, exp.Sub):
            return self.catalog_expression(node.left) - self.catalog_expression(node.right)
        if isinstance(node, (exp.EQ, exp.NEQ)):
            equal = self.catalog_expression(node.left) == self.catalog_expression(node.right)
            return not equal if isinstance(node, exp.NEQ) else equal
        if isinstance(node, exp.And):
            return self.catalog_expression(node.left) and self.catalog_expression(node.right)
        return self._unsupported_catalog_expression(node)

    def _catalog_case(self, node):
        for branch in node.args["ifs"]:
            if self.catalog_expression(branch.this):
                return self.catalog_expression(branch.args["true"])
        return self.catalog_expression(node.args["default"])

    def _catalog_function(self, node):
        name = node.name.lower()
        if name == "format_type":
            return "int4"  # This domain is visible under search_path=app,pg_catalog.
        if name == "repeat":
            return self.catalog_expression(node.expressions[0]) * self.catalog_expression(
                node.expressions[1]
            )
        if name == "quote_ident":
            return self.catalog_expression(node.expressions[0])
        return self._unsupported_catalog_expression(node)

    @staticmethod
    def _unsupported_catalog_expression(node):
        message = f"Unsupported catalog identity expression: {type(node).__name__}"
        raise AssertionError(message)


def shadow_domain_target(monkeypatch):
    database = ShadowDomainDatabase()

    def connect(_alias):
        connection = _DateConnection(database)
        database.opened.append(connection)
        return connection

    monkeypatch.setattr(load_df_module, "get_sql_connection", connect)
    return database
