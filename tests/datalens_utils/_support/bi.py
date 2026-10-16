"""Small synthetic BI metadata and project-file helpers."""

from dataclasses import replace

from datalens_sdk import Dataset, EntryLocation, Source

from tests._support.files import write_json
from tests.datalens_utils._support.sdk import MemoryDataLens, fixture


def dataset():
    return Dataset(
        id="sales",
        name="Sales",
        installation="yacloud",
        location=EntryLocation.path("Offline/Variants"),
        saved_id="revision",
        published_id="revision",
        sources=(
            Source(
                id="source-id",
                source_type="CH_TABLE",
                title="source",
                connection_id="ch",
                connection_type="clickhouse",
                parameters={"db_name": "example", "table_name": "sales"},
            ),
        ),
        source_avatars=({"id": "avatar", "source_id": "source-id"},),
        result_schema=(
            {
                "guid": "category",
                "title": "category",
                "source": "category",
                "avatar_id": "avatar",
                "calc_mode": "direct",
                "cast": "string",
                "data_type": "string",
                "type": "DIMENSION",
                "aggregation": "none",
            },
            {
                "guid": "amount",
                "title": "Amount",
                "calc_mode": "formula",
                "formula": "SUM(1)",
                "cast": "float",
                "data_type": "float",
                "type": "MEASURE",
                "aggregation": "none",
            },
        ),
    )


def write(root, name, value):
    return write_json(root / name, value)


def normalized_sources(value):
    """Read synthetic sources through the public SDK's offline response parser."""
    backend = MemoryDataLens()
    document = fixture("dataset.json")
    document.update(
        id=value.id, key=value.key, savedId=value.saved_id, publishedId=value.published_id
    )
    document["dataset"]["sources"] = [
        {
            "id": source.id,
            "title": source.title,
            "source_type": source.source_type,
            "connection_id": source.connection_id,
            "connection_type": source.connection_type,
            "parameters": dict(source.parameters),
            "raw_schema": list(source.raw_schema),
            "valid": source.valid,
        }
        for source in value.sources
    ]
    backend.documents[value.id] = document
    try:
        return replace(value, sources=backend.client.get.dataset(by_id=value.id).sources)
    finally:
        backend.client.close()


def apply_field_actions(schema, actions):
    """Simulate persisted field updates and direct-field server defaults."""
    rows = [dict(row) for row in schema]
    for action in actions:
        if action["action"] == "add_field":
            rows.append(
                {
                    "aggregation": "none",
                    "type": "DIMENSION",
                    **action["field"],
                    "calc_mode": "direct",
                }
            )
        elif action["action"] == "update_field":
            for row in rows:
                if row["guid"] == action["field"]["guid"]:
                    row.update(action["field"])
    return tuple(rows)


def assert_unmanaged_remnants(value, case, rule):
    """Owned removal must retain unrelated persisted rules, joins and filters."""
    if case == "rls":
        assert value.rls2 == {"category": [rule("unmanaged")]}
    elif case == "join":
        assert value.relations == ({"id": "unmanaged-join"},)
    else:
        assert value.default_filters == (
            {"id": "unmanaged-filter", "field_guid": "amount", "default_filters": []},
        )
