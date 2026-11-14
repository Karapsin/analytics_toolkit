"""Native INET storage over the real psycopg2 execute_values serializer."""

from __future__ import annotations

import datetime as dt
import ipaddress

from sqlglot import exp

from tests.sql._support.gp_temporal_values import TemporalConnection, TemporalDatabase
from tests.sql._support.load_table import load_df_module
from tests.sql._support.transfer_not_null import TransferDatabase


class InetDatabase(TemporalDatabase):
    def __init__(self, native_type):
        super().__init__(native_type)
        seed = ipaddress.ip_interface("198.51.100.2/24")
        self.target.rows = [{"payload": [seed] if native_type.endswith("[]") else seed}]
        self.opened = []

    @classmethod
    def decode_value(cls, node, native_type):
        while isinstance(node, exp.Cast):
            node = node.this
        if isinstance(node, exp.Null):
            return None
        if native_type.endswith("[]"):
            assert isinstance(node, exp.Array)
            return [cls.decode_value(item, native_type[:-2]) for item in node.expressions]
        assert isinstance(node, exp.Literal)
        assert node.is_string
        if native_type.upper() in {"TIMETZ", "TIME WITH TIME ZONE"}:
            value = dt.time.fromisoformat(node.this)
            offset = value.utcoffset() or dt.timedelta(0)
            # This fixture has a UTC server session. Native TIMETZ keeps the
            # stored offset as a tie-breaker after comparing UTC clock values.
            wall = ((value.hour * 60 + value.minute) * 60 + value.second) * 1_000_000
            return wall + value.microsecond, int(offset.total_seconds())
        if native_type.upper() != "INET":
            assert native_type.upper() in {"TEXT", "VARCHAR"}
            return node.this
        # The oracle covers the selected full-form IPv4/IPv6 fixtures. It keeps
        # host bits and prefix length, rather than reducing INET to a network.
        return ipaddress.ip_interface(node.this)

    def _insert(self, tree, identity, params):
        if isinstance(tree.expression, exp.Values):
            return super()._insert(tree, identity, params)
        return TransferDatabase._insert(self, tree, identity, params)

    def select(self, tree, outer=None):
        joins = tree.args.get("joins")
        if not joins:
            return super().select(tree, outer)
        # Key-validated append asks whether the real stage intersects the target.
        # Evaluate its emitted native null-safe join over typed stored INETs.
        assert len(joins) == 1
        left = tree.args["from_"].this
        right = joins[0].this
        matches = []
        for left_row in self.table(left).rows:
            for right_row in self.table(right).rows:
                row = {
                    **{f"{left.alias_or_name}.{key}": value for key, value in left_row.items()},
                    **{f"{right.alias_or_name}.{key}": value for key, value in right_row.items()},
                }
                if self.matches(joins[0].args["on"], row):
                    matches.append((1,))
        return matches[:1], ["1"]


def native_inet_target(monkeypatch, native_type="INET", *, exists=True, empty=False):
    database = InetDatabase(native_type)
    if not exists:
        database.tables.clear()
    elif empty:
        database.target.rows = []

    def connect(_key):
        connection = TemporalConnection(database)
        database.opened.append(connection)
        return connection

    monkeypatch.setattr(load_df_module, "get_sql_connection", connect)
    return database
