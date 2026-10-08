"""Technical SDK context; resource definitions live in the root recipe."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

from analytics_toolkit.datalens_utils.auth.client import datalens_client
from analytics_toolkit.datalens_utils.errors import DataLensUtilsError
from analytics_toolkit.datalens_utils.session import current_session as session

from .folders import ensure_resource_folders, ensure_target_folder
from .store import ResourceStore


@dataclass
class DashboardContext:
    client: object
    folder: object
    connection: object
    resources: ResourceStore
    source_tables: dict[str, str]


@contextmanager
def dashboard_context(  # noqa: PLR0913
    *,
    dashboard_path: Any,
    source_tables: Any,
    resource_folders: Any,
    connection_name: Any,
    connection_id: Any,
    chart_definitions: Any,
    dataset_definitions: Any,
    dashboard_id: Any = None,
) -> Iterator[Any]:
    folder_path, _ = dashboard_path.rsplit("/", 1)
    with datalens_client() as client:
        if "CH_SUBSELECT" not in client.capabilities["dataset_sources"]:
            message = "The installation does not support projected ClickHouse dataset sources."
            raise DataLensUtilsError(message)
        for family in ("wizard", "ql", "editor"):
            requested = {
                definition["type"]
                for definition in chart_definitions.values()
                if definition["family"] == family
            }
            absent = requested - set(client.capabilities["chart_factories"][family])
            if absent:
                message = f"Unsupported {family} chart factories: {sorted(absent)}."
                raise DataLensUtilsError(message)
        connection = client.get.connection(by_id=connection_id)
        if connection.name != connection_name:
            message = (
                "Configured connection "
                f"{connection.name!r}"
                " differs from selected connection "
                f"{connection_name!r}"
                "."
            )
            raise DataLensUtilsError(message)
        if connection.type != "clickhouse" or connection.raw.get("raw_sql_level") != "dashsql":
            message = (
                "The selected ClickHouse connection must already permit SQL-to-read (dashsql)."
            )
            raise DataLensUtilsError(message)
        folder = ensure_target_folder(client=client, path=folder_path)
        resources = ResourceStore(
            folder,
            {
                "organization_id": session().runtime["organization_id"],
                "folder_path": folder_path,
                "connection_id": connection.id,
                "source_tables": source_tables,
            },
            allow_folder_move=True,
        )
        resources.set_folders(
            ensure_resource_folders(client=client, parent=folder, names=resource_folders)
        )
        configured = {
            **{
                f"dataset:{key}": {**definition, "scope": "dataset"}
                for key, definition in dataset_definitions.items()
            },
            **{
                f"chart:{key}": {**definition, "scope": "widget"}
                for key, definition in chart_definitions.items()
            },
            "dashboard": {
                "id": dashboard_id,
                "name": dashboard_path.rsplit("/", 1)[-1],
                "scope": "dash",
            },
        }
        resources.seed_configured_ids(configured)
        yield DashboardContext(client, folder, connection, resources, source_tables)
