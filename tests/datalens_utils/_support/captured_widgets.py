"""Public SDK source factories for individual and grouped chart placements."""

from datalens_sdk import DashboardChartTab, DashboardTab


def widget_source(source, grouped, *, supported_metadata=False):
    options = (
        {"description": "Authored description", "hint": "Authored hint"}
        if supported_metadata
        else {}
    )
    tab = DashboardTab("One", tab_id="one")
    if grouped:
        tab.add_chart_group(
            [
                DashboardChartTab(chart=source.chart, title="First", **options),
                DashboardChartTab(chart=source.chart, title="Second", **options),
            ],
            item_id="placement",
            at=(0, 0, 12, 6),
        )
    else:
        tab.add_chart(source.chart, item_id="placement", at=(0, 0, 12, 6), **options)
    dashboard = (
        source.api.client.create.dashboard(name="Source", location=source.api.folder)
        .add_tab(tab)
        .build()
    )
    source.dashboard = dashboard
    source.entities[dashboard.id] = dashboard
    source.kinds[dashboard.id] = "dash"
    return dashboard
