"""GP float text versus actual send-format bytes without changing source GUCs."""

from __future__ import annotations

import struct
from dataclasses import replace

from tests.sql._support import gp_source_codecs as source_support


class FloatCursor(source_support.SourceCursor):
    def execute(self, query):
        super().execute(query)
        self.query = query
        self.probe = "WHERE 1 = 0" in query
        self.binary = "pg_catalog.float" in query or "pg_catalog.array_send" in query
        if self.binary:
            self.description = [("_atk_0", 17, None, None, None, None, None)]
        if not self.probe:
            self.connection.executed_inputs.append(query)
            assert self.connection.original_query in query
            assert "current_setting('extra_float_digits') = '0'" in query
            assert not query.lstrip().upper().startswith("SET")

    def fetchmany(self, size):
        if self.probe or self.index:
            return []
        self.index += 1
        connection = self.connection
        if self.binary:
            value = memoryview(connection.wire)
        else:
            caster = self.casters[connection.oid]
            value = connection.callbacks[id(caster)](connection.documents[0], None)
        connection.decoded.append((value,))
        return [(value,)]


def float_source(monkeypatch, oid):
    monkeypatch.setattr(source_support, "SourceCursor", FloatCursor)
    native = {700: "REAL", 701: "DOUBLE PRECISION", 1022: "DOUBLE PRECISION[]"}[oid]
    value = 1.23456789012345
    wire = struct.pack("!f" if oid == 700 else "!d", value)
    if oid == 700:
        value = struct.unpack("!f", wire)[0]
    text = "1.23457"
    if oid == 1022:
        text = "[-1:0]={1.23457,NULL}"
        wire = struct.pack("!iiiiii", 1, 1, 701, 2, -1, 8) + wire + struct.pack("!i", -1)
    scenario, source = source_support.install_source_transfer(
        monkeypatch, native, oid, [text], frozen=False
    )
    source.wire = wire
    source.executed_inputs = []
    source.original_query = (
        "SELECT payload AS id FROM source WHERE current_setting('extra_float_digits') = '0'"
    )
    scenario.options = replace(scenario.options, source_sql=source.original_query)
    return scenario, source, value
