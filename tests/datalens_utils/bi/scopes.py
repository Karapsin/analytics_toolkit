"""Scoped writes read the prerequisites of every affected dashboard."""

import copy
from contextlib import nullcontext

from analytics_toolkit.datalens_utils import bi_engine
from analytics_toolkit.datalens_utils.recipe import load_registry
from analytics_toolkit.datalens_utils.resources.bi_store import BIResourceStore
from datalens_sdk import Connection

from tests.datalens_utils._support.bi import dataset, write

from .dashboard import setup_dashboard


def test_apply_chart_loads_unrelated_chart_and_dataset_without_writing_them(
    bi_project, monkeypatch
):
    api, registry, store, charts, datasets = setup_dashboard(bi_project)
    root = bi_project.paths.project_root
    definition = copy.deepcopy(registry.resources["dataset:sales"].definition)
    definition.update(name="Other sales", id="other")
    write(
        root,
        "configs/DL objects/datasets.json",
        {"sales": registry.resources["dataset:sales"].definition, "other": definition},
    )
    other = copy.copy(dataset())
    other.id, other.name = "other", "Other sales"
    second = (
        api.client.create.wizard_chart.line(name="Second", location=api.folder)
        .dataset(other)
        .x([other.fields.by_name("category")])
        .y([other.fields.by_name("Amount")])
        .build()
    )
    second_definition = {
        **registry.resources["chart:trend"].definition,
        "key": "second",
        "id": second.id,
        "name": "Second",
        "title": "Second",
        "dataset": "other",
    }
    write(root, "configs/DL objects/charts/wizard/line/second.json", second_definition)
    write(
        root, "configs/UI/layout.json", {"main": {"trend": [0, 0, 12, 6], "second": [12, 0, 12, 6]}}
    )
    dashboard = api.client.get.dashboard(
        by_id=registry.resources["dashboard:main"].definition["id"]
    )
    dashboard.update.apply_layout({"foreign": (0, 6, 12, 2)}, tab="main").mode("publish").execute()
    registry = load_registry()
    store = BIResourceStore(registry)
    store.set_location(api.folder)
    connection = Connection(id="ch", name="CH", type="clickhouse", installation="yacloud")
    entities = {
        "dataset:sales": datasets["sales"],
        "dataset:other": other,
        "chart:trend": charts["trend"],
        "chart:second": second,
        "connection:ch": connection,
        "dashboard:main": api.client.get.dashboard(by_id=dashboard.id),
    }
    for key, entity in entities.items():
        store.checkpoint(
            store.internal_key(key), entity, scope=store.scope(registry.resources[key].kind)
        )
        store.remember(key, entity)
    # A presentation edit to A affects the dashboard, whose read closure also
    # needs B and B's dataset. Their metadata and inputs are already verified.
    first = registry.resources["chart:trend"].definition
    first["title"] = "Changed trend"
    write(root, "configs/DL objects/charts/wizard/line/trend.json", first)
    loaded = []

    def read(_client, _store, _registry, keys, **_kwargs):
        loaded.extend(keys)
        return {key: entities[key] for key in keys}

    monkeypatch.setattr(bi_engine, "datalens_client", lambda: nullcontext(api.client))
    monkeypatch.setattr(bi_engine, "read_entities", read)
    monkeypatch.setattr(bi_engine, "resolve_location", lambda *_args: api.folder)
    before = len(api.writes)
    result = bi_engine.run("apply", resource_keys=["chart:trend"])
    assert {"chart:second", "dataset:other"}.issubset(loaded)
    assert result["resource_ids"]["chart:second"] == second.id
    updates = [body for operation, body in api.writes[before:] if operation == "updateWizardChart"]
    assert all(body.get("entry", body).get("entryId") != second.id for body in updates)
    api.client.close()
