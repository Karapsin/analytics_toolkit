"""Small synthetic BI metadata and project-file helpers."""

from datalens_sdk import Dataset, EntryLocation, Source

from tests._support.files import write_json


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
