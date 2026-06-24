from __future__ import annotations


PARTITION_REPLACEMENT_UPSERT_BACKENDS = frozenset({"trino", "ch"})


def uses_partition_replacement_upsert(connection_type: str) -> bool:
    return connection_type in PARTITION_REPLACEMENT_UPSERT_BACKENDS


def is_trino_backend(connection_type: str) -> bool:
    return connection_type == "trino"


def is_clickhouse_backend(connection_type: str) -> bool:
    return connection_type == "ch"
