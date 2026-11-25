"""Reachable independent replicas with truthful physical-table presence."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy

from tests.sql._support.reconfigure_pair_scope import PairScopeClient


class SourcePresenceClient(PairScopeClient):
    def __init__(self, *, complete):
        super().__init__()
        self.clusters["storage"] = (self.coordinator, self.remote)
        self.host_tables[self.remote].pop(self.source_table)
        physical = "analytics.events_shard"
        physical_create = deepcopy(self.host_tables[self.remote][physical].create)
        self._create(self.coordinator, physical_create, [(1,), (2,), (3,)])
        if not complete:
            self.host_tables[self.remote].pop(physical)
        self.created.clear()
        self.reachability = []
        self.physical_presence = []

    def _metadata(self, name, hosts):
        records = super()._metadata(name, hosts)
        if name == "system.clusters":
            replicas = {
                self.host_endpoints[host][0]: index
                for index, host in enumerate(self.clusters["storage"], 1)
            }
            for record in records:
                if record["cluster"] == "storage":
                    record.update(shard_num=1, replica_num=replicas[record["host_name"]])
        elif name == "system.one":
            self.reachability.append(
                (tuple(hosts), tuple(record["_shard_num"] for record in records))
            )
        elif name == "system.tables":
            positions = tuple(
                record["_shard_num"]
                for record in records
                if (record["database"], record["name"]) == ("analytics", "events_shard")
            )
            self.physical_presence.append((tuple(hosts), positions))
        return records


class MetadataObserver:
    def __init__(self):
        self.statements = []

    @contextmanager
    def submission(self, statement):
        self.statements.append(statement)
        yield
