"""Backend-owned names-only listing providers for SQL Explorer."""

from __future__ import annotations

from typing import TYPE_CHECKING

from analytics_toolkit.sql.backends.metadata import (
    CH_CURRENT_DATABASE_QUERY,
    CH_DATABASES_QUERY,
    GP_PARTITION_CAPABILITIES_QUERY,
    TRINO_CURRENT_NAMESPACE_QUERY,
    build_ch_completion_query,
    build_gp_completion_query,
    build_gp_reference_query,
    build_gp_search_path_query,
    build_trino_completion_query,
)
from analytics_toolkit.sql.ddl.identifiers import quote_identifier

if TYPE_CHECKING:
    import pandas as pd

    from analytics_toolkit.sql.connection.config import ConnectionConfig

    from .completion import MetadataProvider


def _metadata_frame(connection_key: str, query: str) -> pd.DataFrame:
    # Preserve the established completion facade's injectable read boundary.
    from . import completion  # noqa: PLC0415

    return completion._metadata_frame(connection_key, query)  # noqa: SLF001


def _first_column_values(frame: pd.DataFrame) -> tuple[str, ...]:
    from . import completion  # noqa: PLC0415

    return completion._first_column_values(frame)  # noqa: SLF001


def get_connection_config(connection_key: str) -> ConnectionConfig:
    from . import completion  # noqa: PLC0415

    return completion.get_connection_config(connection_key)


class GreenplumCompletionProvider:
    def __init__(self) -> None:
        self._partition_catalog: str | None = None

    def resolve_reference(
        self,
        connection_key: str,
        name: str,
        *,
        search_path: list[str] | None = None,
        candidates: list[tuple[str, str]] | None = None,
    ) -> tuple[str, str] | None:
        if candidates:
            frame = _metadata_frame(connection_key, build_gp_search_path_query(search_path))
            # Search-path order is semantic; completion normalization sorts names.
            path = tuple(str(row[0]) for row in frame.itertuples(index=False, name=None))
            return next((("", schema) for schema in path if ("", schema) in candidates), None)
        values = _first_column_values(
            _metadata_frame(connection_key, build_gp_reference_query(name, search_path))
        )
        return ("", values[0]) if values and getattr(values, "complete", True) else None

    def list_tables(
        self,
        *,
        connection_key: str,
        prefix: str,
        schema: str | None = None,
        catalog: str | None = None,
    ) -> tuple[str, ...]:
        del catalog
        if self._partition_catalog is None:
            values = _first_column_values(
                _metadata_frame(connection_key, GP_PARTITION_CAPABILITIES_QUERY)
            )
            self._partition_catalog = values[0] if values else "none"
        return _first_column_values(
            _metadata_frame(
                connection_key, build_gp_completion_query(schema, prefix, self._partition_catalog)
            )
        )

    def list_catalogs(self, *, connection_key: str) -> tuple[str, ...]:
        del connection_key
        return ()

    def list_schemas(
        self,
        *,
        connection_key: str,
        catalog: str | None = None,
    ) -> tuple[str, ...]:
        del catalog
        return _first_column_values(
            _metadata_frame(
                connection_key,
                "SELECT schema_name FROM information_schema.schemata ORDER BY schema_name",
            )
        )


class ClickHouseCompletionProvider:
    def resolve_reference(
        self,
        connection_key: str,
        name: str,
        *,
        search_path: list[str] | None = None,
        candidates: list[tuple[str, str]] | None = None,
    ) -> tuple[str, str] | None:
        del name, search_path, candidates
        values = _first_column_values(_metadata_frame(connection_key, CH_CURRENT_DATABASE_QUERY))
        return ("", values[0]) if values and getattr(values, "complete", True) else None

    def list_tables(
        self,
        *,
        connection_key: str,
        prefix: str,
        schema: str | None = None,
        catalog: str | None = None,
    ) -> tuple[str, ...]:
        del catalog
        return _first_column_values(
            _metadata_frame(connection_key, build_ch_completion_query(schema, prefix))
        )

    def list_catalogs(self, *, connection_key: str) -> tuple[str, ...]:
        del connection_key
        return ()

    def list_schemas(
        self,
        *,
        connection_key: str,
        catalog: str | None = None,
    ) -> tuple[str, ...]:
        del catalog
        return _first_column_values(_metadata_frame(connection_key, CH_DATABASES_QUERY))


class TrinoCompletionProvider:
    def resolve_reference(
        self,
        connection_key: str,
        name: str,
        *,
        search_path: list[str] | None = None,
        candidates: list[tuple[str, str]] | None = None,
    ) -> tuple[str, str] | None:
        del name, search_path, candidates
        frame = _metadata_frame(connection_key, TRINO_CURRENT_NAMESPACE_QUERY)
        if frame.empty or frame.iloc[0].isna().any():
            return None
        return str(frame.iloc[0, 0]), str(frame.iloc[0, 1])

    def list_tables(
        self,
        *,
        connection_key: str,
        prefix: str,
        schema: str | None = None,
        catalog: str | None = None,
    ) -> tuple[str, ...]:
        catalog = catalog or getattr(get_connection_config(connection_key), "catalog", None)
        if not catalog:
            message = "Trino table completion requires a catalog."
            raise ValueError(message)
        return _first_column_values(
            _metadata_frame(
                connection_key,
                build_trino_completion_query(quote_identifier(catalog, "trino"), schema, prefix),
            )
        )

    def list_catalogs(self, *, connection_key: str) -> tuple[str, ...]:
        return _first_column_values(_metadata_frame(connection_key, "SHOW CATALOGS"))

    def list_schemas(
        self,
        *,
        connection_key: str,
        catalog: str | None = None,
    ) -> tuple[str, ...]:
        query = "SHOW SCHEMAS"
        if catalog is not None:
            query += f" FROM {quote_identifier(catalog, 'trino')}"
        return _first_column_values(_metadata_frame(connection_key, query))


def provider_for_backend(backend: str) -> MetadataProvider:
    normalized = backend.casefold()
    if normalized == "trino":
        return TrinoCompletionProvider()
    if normalized in {"ch", "clickhouse"}:
        return ClickHouseCompletionProvider()
    return GreenplumCompletionProvider()
