"""Offline live-source doubles and public import/project entry points."""

from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from analytics_toolkit.datalens_utils import (
    DataLensAuthConfig,
    DataLensProject,
    TargetLocation,
    import_dashboard,
)
from datalens_sdk import Connection, DashboardTab, EntryLocation

from tests.datalens_utils._support.sdk import MemoryDataLens


@pytest.fixture
def import_source():
    api = MemoryDataLens()
    dataset = api.seed_dataset("source")
    field = next(iter(dataset.fields))
    chart = (
        api.client.create.wizard_chart.flat_table(name="Source chart", location=api.chart_folder)
        .dataset(dataset)
        .columns([field])
        .build()
    )
    dashboard = (
        api.client.create.dashboard(name="Source", location=api.folder)
        .add_tab(
            DashboardTab("One", tab_id="one").add_chart(chart, item_id="first", at=(0, 0, 12, 6))
        )
        .build()
    )
    chart = api.client.get.wizard_chart(by_id=chart.id, branch="published")
    dashboard = api.client.get.dashboard(by_id=dashboard.id, branch="published")
    connection = Connection(
        id=dataset.sources[0].connection_id,
        name="Existing",
        type="clickhouse",
        installation="yacloud",
        location=EntryLocation.path("Other"),
    )
    entities = {item.id: item for item in (dataset, chart, dashboard, connection)}
    kinds = {
        dataset.id: "dataset",
        chart.id: "widget",
        dashboard.id: "dash",
        connection.id: "connection",
    }
    reads = []

    def get(kind, **kwargs):
        reads.append((kind, kwargs))
        return entities[kwargs["by_id"]]

    def entries(**kwargs):
        return iter(
            SimpleNamespace(
                id=identifier,
                name=entities[identifier].name,
                key=entities[identifier].key,
                workbook_id=entities[identifier].workbook_id,
                scope=kinds[identifier],
                type=getattr(entities[identifier], "wire_type", None),
                published_id=getattr(entities[identifier], "published_id", None),
                saved_id=getattr(entities[identifier], "saved_id", None),
            )
            for identifier in kwargs["ids"]
        )

    client = SimpleNamespace(
        INSTALLATION=api.client.INSTALLATION,
        create=api.client.create,
        capabilities=api.client.capabilities,
        navigation=SimpleNamespace(get_entries=entries),
        get=SimpleNamespace(
            **{
                kind: lambda kind=kind, **kwargs: get(kind, **kwargs)
                for kind in ("dashboard", "chart", "wizard_chart", "dataset", "connection")
            }
        ),
    )
    yield SimpleNamespace(
        api=api,
        client=client,
        entities=entities,
        dashboard=dashboard,
        chart=chart,
        dataset=dataset,
        reads=reads,
    )
    api.client.close()


def run_import(source, tmp_path, **kwargs):
    return import_dashboard(
        source.dashboard.id,
        tmp_path / "project",
        auth=DataLensAuthConfig(organization_id="offline"),
        runtime_root=tmp_path / "runtime",
        client_factory=lambda _: nullcontext(source.client),
        **kwargs,
    )


def imported_project(source, tmp_path):
    return DataLensProject(
        tmp_path / "project",
        tmp_path / "runtime",
        DataLensAuthConfig(organization_id="offline").deployment(
            "Source", TargetLocation.path("Offline/Variants")
        ),
        client_factory=lambda _: nullcontext(source.client),
    )
