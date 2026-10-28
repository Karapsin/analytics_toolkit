"""Imported locations never leak into ordinary resource adoption or creation."""

from types import SimpleNamespace

import pytest
from analytics_toolkit.datalens_utils import BIProjectDeployment, TargetLocation, bi_engine
from analytics_toolkit.datalens_utils.errors import DataLensUtilsError
from analytics_toolkit.datalens_utils.import_source import location
from analytics_toolkit.datalens_utils.recipe import load_registry
from analytics_toolkit.datalens_utils.resources.bi_store import BIResourceStore
from datalens_sdk import Connection, DashboardTab, EntryLocation, Workbook

from tests.datalens_utils._support.bi import dataset, write
from tests.datalens_utils._support.sdk import FOLDER_PATH, MemoryDataLens


def deployment_location(api, kind, enumerations):
    if kind == "path":
        base = api.folder

        class Destination(type(base)):
            def list_entries(self):
                enumerations.append(self.key)
                return super().list_entries()

        return Destination(id=base.id, name=base.name, key=base.key), TargetLocation.path(
            FOLDER_PATH
        )

    class DestinationWorkbook(Workbook):
        def list_entries(self):
            enumerations.append(self.id)
            return [
                SimpleNamespace(
                    id=identifier, name=value["entry"]["name"], scope=value["entry"]["scope"]
                )
                for identifier, value in api.documents.items()
                if value.get("entry", {}).get("workbookId") == self.id
            ]

    return DestinationWorkbook(id="default-book", name="Default"), TargetLocation.workbook(
        by_id="default-book"
    )


@pytest.mark.parametrize("default_kind", ["path", "workbook"])
@pytest.mark.parametrize("imported_kind", ["path", "workbook"])
def test_mixed_reconciliation_restores_default_for_existing_new_and_adopted_resources(
    bi_project, monkeypatch, default_kind, imported_kind
):
    api = MemoryDataLens()
    enumerations = []
    destination, target = deployment_location(api, default_kind, enumerations)
    bi_project.deployment = BIProjectDeployment("Dashboard", target, organization_id="offline")
    ds = dataset()
    roles = ["a_imported", "b_existing", "c_imported", "d_new", "e_adopted"]
    base_definition = load_registry().resources["chart:trend"].definition
    root = bi_project.paths.project_root
    (root / "configs/DL objects/charts/wizard/line/trend.json").unlink()
    charts, expected = {}, {}
    for role in roles:
        imported = role.endswith("imported")
        where = (
            EntryLocation.path("Foreign/" + role)
            if imported and imported_kind == "path"
            else EntryLocation.workbook("foreign-" + role)
            if imported
            else destination
        )
        definition = {**base_definition, "key": role, "name": role, "title": role}
        definition.pop("id")
        if role != "d_new":
            chart = (
                api.client.create.wizard_chart.line(name=role, location=where)
                .dataset(ds)
                .x([ds.fields.by_name("category")])
                .y([ds.fields.by_name("Amount")])
                .build()
            )
            charts[role] = chart
            if role != "e_adopted":
                definition["id"] = chart.id
            if imported:
                definition["location"] = location(chart)
            expected[role] = location(chart)
        else:
            expected[role] = {"kind": target.kind, "value": target.value, "reference": "id"}
        write(root, "configs/DL objects/charts/wizard/line/" + role + ".json", definition)
    tab = DashboardTab("Main", tab_id="main").add_chart(
        charts["b_existing"], item_id="b_existing", at=(0, 0, 12, 6)
    )
    dashboard = (
        api.client.create.dashboard(name="Dashboard", location=destination).add_tab(tab).build()
    )
    write(root, "configs/DL objects/dashboard.json", {"id": dashboard.id})
    write(
        root,
        "configs/UI/layout.json",
        {"main": {role: [0, index * 6, 12, 6] for index, role in enumerate(roles)}},
    )
    monkeypatch.setattr(bi_engine, "resolve_location", lambda *_args: destination)
    with bi_project.session():
        registry = load_registry()
        store = BIResourceStore(registry)
        entities = {
            "dataset:sales": ds,
            "connection:ch": Connection(id="ch", name="CH", type="clickhouse"),
            "dashboard:main": dashboard,
            **{"chart:" + key: entity for key, entity in charts.items() if key != "e_adopted"},
        }
        before = len(api.writes)
        writes = {"chart:" + role for role in roles} | {"dashboard:main"}
        bi_engine.apply_resources(api.client, store, registry, writes, entities)
        assert [key for key in registry.order if key in writes] == [
            "chart:" + role for role in roles
        ] + ["dashboard:main"]
        for role in roles:
            assert location(entities["chart:" + role]) == expected[role]
        assert entities["chart:e_adopted"].id == charts["e_adopted"].id
        assert entities["chart:b_existing"].id == charts["b_existing"].id
        assert location(entities["dashboard:main"]) == {
            "kind": target.kind,
            "value": target.value,
            "reference": "id",
        }
        assert store.folder is destination
        assert len(enumerations) == 1
        created = [
            body for operation, body in api.writes[before:] if operation == "createWizardChart"
        ]
        assert len(created) == 1
        assert store.entries["widget"]
    api.client.close()


def test_missing_default_refuses_to_reuse_an_imported_location(bi_project):
    registry = load_registry()
    store = BIResourceStore(registry)
    store.use_resource_location(
        SimpleNamespace(definition={"location": {"kind": "path", "value": "Foreign"}})
    )
    with pytest.raises(DataLensUtilsError, match="default deployment location"):
        store.use_resource_location(registry.resources["chart:trend"])
