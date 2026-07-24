from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from sqlglot import exp, parse_one
from sqlglot.errors import ParseError

from ..backends import get_backend_adapter


@dataclass(frozen=True)
class TableIdentifier:
    parts: tuple[str, ...]
    quoted: tuple[bool, ...]

    @classmethod
    def parse(cls, table_name: str, connection_type: str) -> "TableIdentifier":
        dialect = get_backend_adapter(connection_type).sqlglot_dialect
        try:
            table = parse_one(table_name, read=dialect, into=exp.Table)
        except ParseError as exc:
            raise ValueError(f"Invalid table name: {table_name}") from exc
        if not isinstance(table, exp.Table) or not isinstance(table.this, exp.Identifier):
            raise ValueError(f"Invalid table name: {table_name}")

        identifiers = _table_identifiers(table)
        if not identifiers:
            raise ValueError(f"Invalid table name: {table_name}")
        return cls(
            parts=tuple(str(identifier.this) for identifier in identifiers),
            quoted=tuple(bool(identifier.args.get("quoted")) for identifier in identifiers),
        )

    @property
    def relation(self) -> str:
        return self.parts[-1]

    def with_relation_suffix(self, suffix: str) -> "TableIdentifier":
        if not suffix:
            raise ValueError("suffix must not be empty.")
        return TableIdentifier(
            parts=(*self.parts[:-1], f"{self.relation}{suffix}"),
            quoted=self.quoted,
        )

    def render(self, connection_type: str) -> str:
        return ".".join(
            quote_identifier_part(part, connection_type, quoted=quoted)
            for part, quoted in zip(self.parts, self.quoted)
        )

    def render_quoted(self, connection_type: str) -> str:
        return ".".join(
            quote_identifier_part(part, connection_type, quoted=True)
            for part in self.parts
        )


def parse_table_identifier(table_name: str, connection_type: str) -> TableIdentifier:
    return TableIdentifier.parse(table_name, connection_type)


def quote_identifier_part(
    identifier: str,
    connection_type: str,
    *,
    quoted: bool = True,
) -> str:
    if not quoted:
        return identifier
    return cast("str", get_backend_adapter(connection_type).quote_identifier(identifier))


def sqlglot_dialect(connection_type: str) -> str:
    return cast("str", get_backend_adapter(connection_type).sqlglot_dialect)


def split_gp_table_name(table_name: str) -> tuple[str, str]:
    try:
        identifier = TableIdentifier.parse(table_name, "gp")
    except ValueError as exc:
        raise ValueError(f"Invalid Greenplum table name: {table_name}") from exc
    if len(identifier.parts) == 1:
        return "public", identifier.relation
    if len(identifier.parts) == 2:
        return identifier.parts[0], identifier.relation
    raise ValueError(f"Invalid Greenplum table name: {table_name}")


def split_trino_table_name(
    table_name: str,
    connection_key: str = "trino",
) -> tuple[str, str, str]:
    from ..connection.config import TrinoConfig, get_connection_config

    try:
        parts = TableIdentifier.parse(table_name, "trino").parts
    except ValueError as exc:
        raise ValueError(f"Invalid table name: {table_name}") from exc
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]

    config = get_connection_config(connection_key)
    if not isinstance(config, TrinoConfig):
        raise ValueError("Invalid Trino configuration.")

    if len(parts) == 2:
        if not config.catalog:
            raise ValueError(
                f"Trino table operations for schema-qualified names require "
                f".connections['{config.connection_key}'].catalog."
            )
        return config.catalog, parts[0], parts[1]
    if len(parts) == 1:
        if not config.catalog or not config.schema:
            raise ValueError(
                f"Trino table operations for unqualified names require "
                f".connections['{config.connection_key}'].catalog and schema."
            )
        return config.catalog, config.schema, parts[0]
    raise ValueError(f"Invalid table name: {table_name}")


def _table_identifiers(table: exp.Table) -> list[exp.Identifier]:
    identifiers: list[exp.Identifier] = []
    for key in ("catalog", "db"):
        value = table.args.get(key)
        if value is not None:
            identifiers.append(_require_identifier(value))
    identifiers.append(_require_identifier(table.this))
    return identifiers


def _require_identifier(value: exp.Expression) -> exp.Identifier:
    if not isinstance(value, exp.Identifier):
        raise ValueError(f"Invalid table identifier: {value}")
    return value
