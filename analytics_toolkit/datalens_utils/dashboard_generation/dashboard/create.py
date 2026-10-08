"""Named tabs and dashboard shell creation, with identity-preserving updates."""

from __future__ import annotations

from typing import Any

from datalens_sdk import DashboardTab


def create_tabs(*, definitions: Any) -> Any:
    return {
        role: DashboardTab(definition["title"], tab_id=role, hidden=definition["hidden"])
        for role, definition in definitions.items()
    }


def create_dashboard(
    *, context: Any, path: Any, tabs: Any, description: Any, hide_tabs: Any
) -> Any:
    client, resources = context.client, context.resources
    name = path.rsplit("/", 1)[-1]
    dashboard = resources.existing("dashboard", name, client.get.dashboard, scope="dash")
    if dashboard is None:
        builder = (
            client.create.dashboard(name=name, location=context.folder)
            .description(description)
            .settings(hide_tabs=hide_tabs)
        )
        for tab in tabs.values():
            builder.add_tab(tab)
        dashboard = resources.create("dashboard", name, builder, client.get.dashboard, scope="dash")
    return resources.rename("dashboard", dashboard, name, client.get.dashboard)
