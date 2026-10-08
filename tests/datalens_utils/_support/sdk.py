"""Public SDK fixtures and an HTTP transport that cannot reach DataLens."""

import copy
import json
from types import SimpleNamespace

import httpx
from datalens_sdk import (
    Connection,
    DataLensClientYC,
    Dataset,
    Folder,
    StaticYCIAMAuthProvider,
)

from tests._support.paths import REPO_ROOT

FIXTURES = REPO_ROOT / "tests/datalens_utils/_support/fixtures"
FOLDER_PATH = "Offline/Variants"


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def configured_datasets(definitions):
    """Persisted field snapshots with explicit, stable source/calculation IDs."""
    result = {}
    for role, definition in definitions.items():
        schema = []
        for column, field in definition.get("fields", {}).items():
            schema.append(
                {
                    "guid": f"{role}_{column}",
                    "title": field.get("title", column),
                    "source": column,
                    "cast": field["cast"],
                    "data_type": field["cast"],
                    "initial_data_type": field["cast"],
                    "aggregation": field.get("aggregation", "none"),
                    "type": field.get("kind", "DIMENSION"),
                    "calc_mode": "direct",
                    "formula": "",
                    "valid": True,
                }
            )
        calculations = definition.get("calculations", {})
        if isinstance(calculations, dict):
            calculations = [{"name": name, **field} for name, field in calculations.items()]
        for field in calculations:
            name = field.get("name", field.get("title"))
            schema.append(
                {
                    "guid": f"{role}_calculation_{name.replace(' ', '_')}",
                    "title": name,
                    "source": "",
                    "cast": field["cast"],
                    "data_type": field["cast"],
                    "initial_data_type": field["cast"],
                    "aggregation": field.get("aggregation", "none"),
                    "type": field["kind"],
                    "calc_mode": "formula",
                    "formula": field["formula"],
                    "valid": True,
                }
            )
        for name, parameter in definition.get("parameters", {}).items():
            schema.append(
                {
                    "guid": f"{role}_parameter_{name}",
                    "title": name,
                    "cast": parameter["type"],
                    "data_type": parameter["type"],
                    "aggregation": "none",
                    "type": "DIMENSION",
                    "calc_mode": "parameter",
                    "default_value": parameter["default"],
                    "valid": True,
                }
            )
        result[role] = Dataset(
            id=f"offline-{role}",
            name=definition["name"],
            installation="yacloud",
            description=definition.get("description", ""),
            result_schema=tuple(schema),
        )
    return result


class MemoryDataLens:
    """Exercise public SDK serialization, revisions, and reads without cloud calls.

    Historical entry fixtures supply ordinary server metadata. Every request is
    handled locally, and unknown operations fail rather than falling through.
    """

    def __init__(self):
        self.documents = {}
        self.revisions = {}
        self.writes = []
        self.counter = 0
        self.fail_after_operation = None
        self.fail_next_get = False
        self.connection = Connection(
            id="offline-clickhouse",
            name="Offline ClickHouse",
            type="clickhouse",
            installation="yacloud",
        )
        self.client = DataLensClientYC(
            auth=StaticYCIAMAuthProvider(org_id="offline-org", token="offline-test-token"),
            transport=httpx.MockTransport(self.handle),
        )
        self.folder = self.make_folder(FOLDER_PATH)
        self.chart_folder = self.make_folder(FOLDER_PATH + "/charts")

    def make_folder(self, path):
        backend = self

        class MemoryFolder(Folder):
            def list_entries(self):
                return backend.list_entries(self.key.rstrip("/"))

        return MemoryFolder(
            id="offline-folder-" + path.rsplit("/", 1)[-1],
            name=path.rsplit("/", 1)[-1],
            key=path + "/",
        )

    def list_entries(self, path):
        for identifier, document in self.documents.items():
            entry = document.get("entry", document)
            if entry["key"].rstrip("/").rpartition("/")[0] == path:
                yield SimpleNamespace(
                    id=identifier,
                    scope="dataset" if "dataset" in document else entry["scope"],
                    name=entry["key"].rsplit("/", 1)[-1],
                )

    def seed_dataset(self, role):
        document = fixture("dataset.json")
        identifier = f"offline-dataset-{role}"
        document.update(id=identifier, key=f"{FOLDER_PATH}/datasets/{role}")
        self.documents[identifier] = document
        self.revisions[document["savedId"]] = copy.deepcopy(document)
        return self.client.get.dataset(by_id=identifier)

    def _write(self, identifier, document, mode):
        self.counter += 1
        revision = f"offline-revision-{self.counter}"
        entry = document["entry"]
        previous = self.documents.get(identifier, {}).get("entry", {})
        entry.update(
            entryId=identifier,
            revId=revision,
            savedId=revision,
            publishedId=revision if mode == "publish" else previous.get("publishedId"),
        )
        self.documents[identifier] = document
        self.revisions[revision] = copy.deepcopy(document)
        return document

    def _read(self, identifier, revision=None):
        current = self.documents[identifier]
        document = copy.deepcopy(self.revisions.get(revision, current))
        entry, latest = document.get("entry", document), current.get("entry", current)
        for key in ("key", "savedId", "publishedId"):
            entry[key] = latest[key]
        return document

    def handle(self, request):  # noqa: C901
        operation = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content)
        if operation.startswith("get"):
            if self.fail_next_get:
                self.fail_next_get = False
                message = "Simulated failed re-fetch after persistence"
                raise RuntimeError(message)
            identifier = body.get("chartId") or body.get("dashboardId") or body.get("datasetId")
            revision = body.get("revId")
            if revision is None and body.get("branch") in ("published", "saved"):
                document = self.documents[identifier]
                revision = document.get("entry", document)[body["branch"] + "Id"]
            response = self._read(identifier, revision)
            return httpx.Response(
                200, json=response["entry"] if operation == "getQLChart" else response
            )
        self.writes.append((operation, copy.deepcopy(body)))
        if operation in {
            "createWizardChart",
            "createQLChart",
            "createEditorChart",
            "createDashboard",
        }:
            identifier = f"offline-entry-{self.counter + 1}"
            document = fixture(
                "dashboard.json" if operation == "createDashboard" else "chart_line.json"
            )
            entry = document["entry"]
            incoming = body.get("entry", body)
            entry.update(incoming)
            entry["scope"] = "dash" if operation == "createDashboard" else "widget"
            if operation == "createWizardChart":
                entry["type"] = (
                    "table_wizard_node"
                    if entry["data"]["visualization"]["type"] == "flatTable"
                    else "d3_wizard_node"
                )
            elif operation == "createQLChart":
                entry["type"] = (
                    "table_ql_node"
                    if entry["data"]["visualization"]["id"] == "flatTable"
                    else "d3_ql_node"
                )
            response = self._write(identifier, document, "publish")
        elif operation in {
            "updateWizardChart",
            "updateQLChart",
            "updateEditorChart",
            "updateDashboard",
        }:
            incoming = body.get("entry", body)
            identifier = incoming.get("entryId") or body.get("chartId")
            document = copy.deepcopy(self.documents[identifier])
            if incoming.get("revId"):
                response = self._read(identifier, incoming["revId"])
                response["entry"]["publishedId"] = incoming["revId"]
                self.documents[identifier] = response
            else:
                document["entry"].update(
                    {
                        key: value
                        for key, value in incoming.items()
                        if key in ("data", "annotation", "meta")
                    }
                )
                response = self._write(identifier, document, body.get("mode", "publish"))
        elif operation == "renameEntry":
            entry = self.documents[body["entryId"]]["entry"]
            entry["key"] = entry["key"].rpartition("/")[0] + "/" + body["name"]
            response = []
        else:
            message = f"Unsupported offline SDK operation: {operation}"
            raise AssertionError(message)
        if self.fail_after_operation == operation:
            self.fail_after_operation = None
            self.fail_next_get = True
        return httpx.Response(
            200,
            json=response["entry"] if operation in {"createQLChart", "updateQLChart"} else response,
        )
