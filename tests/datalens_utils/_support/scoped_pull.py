"""Recovered dataset and Wizard snapshots for scoped public pulls."""

import copy
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace

from analytics_toolkit.datalens_utils import bi_engine
from analytics_toolkit.datalens_utils.bi_datasets import (
    remember_inferred_fields,
    source_identities,
)
from analytics_toolkit.datalens_utils.dashboard_generation.charts import wizard
from analytics_toolkit.datalens_utils.recipe import load_registry
from analytics_toolkit.datalens_utils.resources.bi_store import BIResourceStore

from tests.datalens_utils._support.bi import dataset, write
from tests.datalens_utils._support.sdk import MemoryDataLens

DATASET_FILE = "configs/DL objects/datasets.json"
CHART_FILE = "configs/DL objects/charts/wizard/line/trend.json"


def recovered_pull(project, monkeypatch, *, chart_overrides=None, source_sql=None):
    root = project.paths.project_root
    registry = load_registry()
    definition = copy.deepcopy(registry.resources["dataset:sales"].definition)
    definition.pop("id")
    definition["copy_from"] = "source-dataset"
    if source_sql is not None:
        definition["sources"]["source"].update(
            factory="ch_subselect", parameters={}, sql_file="assets/source.sql"
        )
        asset = root / "assets/source.sql"
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.write_text(source_sql, encoding="utf-8")
    write(root, DATASET_FILE, {"sales": definition})
    chart_definition = copy.deepcopy(registry.resources["chart:trend"].definition)
    chart_definition.pop("id")
    chart_definition.update(
        aggregated_measures={
            "Count": {"guid": "count", "field": "category", "aggregation": "count"},
        },
        local_fields=[
            {
                "guid": "local",
                "title": "Local",
                "formula": "'x'",
                "cast": "string",
                "kind": "DIMENSION",
            },
        ],
        hierarchies={"Group": {"guid": "group", "fields": ["category", "local"]}},
        fields={"x": ["Group"], "y": ["Count"]},
    )
    chart_definition.update(copy.deepcopy(chart_overrides or {}))
    write(root, CHART_FILE, chart_definition)
    registry = load_registry()
    store = BIResourceStore(registry)
    original = dataset()
    if source_sql is not None:
        original = replace(
            original,
            sources=(
                replace(
                    original.sources[0],
                    source_type="CH_SUBSELECT",
                    parameters={"subsql": source_sql},
                ),
            ),
        )
    cloud = replace(
        original,
        result_schema=(
            {**original.result_schema[0], "guid": "inferred-category"},
            original.result_schema[1],
        ),
    )
    # Recover and persist source-qualified inference using the production identity helper.
    store.checkpoint("dataset:sales", cloud, scope="dataset")
    owned = store.state["resources"]["dataset:sales"]
    owned.update(
        recipe_field_guids={"category": "category"},
        sources={"source": {"id": "source-id"}},
    )
    remember_inferred_fields(cloud, definition, source_identities(cloud, definition, owned), owned)
    store.remember("dataset:sales", cloud)
    api = MemoryDataLens()
    factory = getattr(api.client.create.wizard_chart, chart_definition["type"])
    builder = factory(name="Trend", location=api.folder).dataset(cloud)
    chart = wizard.configure(
        builder,
        SimpleNamespace(client=api.client),
        {"sales": cloud},
        chart_definition,
        creation=True,
    ).build()
    store.checkpoint("chart:trend", chart, scope="widget")
    store.remember("chart:trend", chart)
    entities = {"dataset:sales": cloud, "chart:trend": chart}
    api.writes.clear()

    def read_entities(client, resources, selected_registry, selected, **kwargs):
        return {key: value for key, value in entities.items() if key in selected}

    monkeypatch.setattr(bi_engine, "read_entities", read_entities)
    monkeypatch.setattr(project, "client_factory", lambda session: nullcontext(api.client))
    return api, entities, store.path
