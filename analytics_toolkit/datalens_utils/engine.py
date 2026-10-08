"""The full identity-preserving recipe, independent of any dashboard launcher."""

from __future__ import annotations

from typing import Any

from .session import current_session as session
from .settings import read_chart_definitions, read_config, source_tables


def validate() -> Any:
    from .dashboard_generation.dashboard.configuration import read_contents  # noqa: PLC0415
    from .validation.recipe import validate_recipe, validate_resource_name  # noqa: PLC0415

    validate_resource_name(session().deployment.dashboard_name)
    return validate_recipe(
        read_config("DL objects/datasets.json"),
        read_chart_definitions(),
        read_config("UI/tabs.json"),
        read_contents(),
    )


def reconcile() -> Any:
    deployment = session().deployment
    from .dashboard_generation import (  # noqa: PLC0415
        create_charts,
        create_dashboard,
        create_datasets,
        create_tabs,
        populate_dashboard,
        read_contents,
    )
    from .editing.commands import remember_full  # noqa: PLC0415
    from .resources.context import dashboard_context  # noqa: PLC0415
    from .validation.checks import record_configured_ids, verify_and_export  # noqa: PLC0415
    from .validation.recipe import validate_recipe, validate_resource_name  # noqa: PLC0415

    validate()
    settings = read_config("DL objects/dashboard.json")
    dataset_definitions = read_config("DL objects/datasets.json")
    chart_definitions = read_chart_definitions()
    tab_definitions = read_config("UI/tabs.json")
    contents = read_contents()
    tables = source_tables(dataset_definitions)
    validate_recipe(dataset_definitions, chart_definitions, tab_definitions, contents)
    validate_resource_name(deployment.dashboard_name)
    dashboard_path = f"{deployment.target_path.strip('/')}/{deployment.dashboard_name}"

    with dashboard_context(
        dashboard_path=dashboard_path,
        source_tables=tables,
        resource_folders=settings["folders"],
        connection_name=deployment.connection_name,
        connection_id=deployment.connection_id,
        chart_definitions=chart_definitions,
        dataset_definitions=dataset_definitions,
        dashboard_id=settings.get("id"),
    ) as context:
        datasets = create_datasets(context=context, definitions=dataset_definitions)
        tabs = create_tabs(definitions=tab_definitions)
        dashboard = create_dashboard(
            context=context,
            path=dashboard_path,
            tabs=tabs,
            description=settings["description"],
            hide_tabs=settings["hide_tabs"],
        )
        charts = create_charts(context=context, datasets=datasets, definitions=chart_definitions)
        dashboard = populate_dashboard(
            context=context,
            dashboard=dashboard,
            datasets=datasets,
            charts=charts,
            tab_definitions=tab_definitions,
            contents=contents,
            chart_definitions=chart_definitions,
            description=settings["description"],
            hide_tabs=settings["hide_tabs"],
        )
        dashboard = verify_and_export(
            context=context,
            dashboard=dashboard,
            datasets=datasets,
            charts=charts,
            dataset_definitions=dataset_definitions,
            chart_definitions=chart_definitions,
            tab_definitions=tab_definitions,
            contents=contents,
            description=settings["description"],
            hide_tabs=settings["hide_tabs"],
        )
        record_configured_ids(dashboard, datasets=datasets, charts=charts)
        remember_full(
            deployment.as_dict(), dashboard, context.verified_datasets, context.verified_charts
        )
        session().emit(f"Dashboard path: {dashboard_path}")
        session().emit(f"Dashboard URL: https://datalens.ru/{dashboard.id}")
    return {
        "dashboard_id": dashboard.id,
        "dashboard_url": "https://datalens.ru/" + dashboard.id,
        "resource_ids": {
            "dashboard": dashboard.id,
            **{"dataset:" + key: value.id for key, value in datasets.items()},
            **{"chart:" + key: value.id for key, value in charts.items()},
        },
    }
