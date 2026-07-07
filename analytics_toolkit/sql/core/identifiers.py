from __future__ import annotations

from dataclasses import dataclass

from sqlglot import exp, parse_one

from ..backends import get_backend_adapter


@dataclass(frozen=True)
class TableIdentifier:
    parts: tuple[str, ...]
    quoted: tuple[bool, ...]

    @classmethod
    def parse(cls, table_name: str, connection_type: str) -> "TableIdentifier":
        dialect = get_backend_adapter(connection_type).sqlglot_dialect
        table = parse_one(table_name, read=dialect, into=exp.Table)
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
    return get_backend_adapter(connection_type).quote_identifier(identifier)


def sqlglot_dialect(connection_type: str) -> str:
    return get_backend_adapter(connection_type).sqlglot_dialect


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
