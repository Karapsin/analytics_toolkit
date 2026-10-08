"""Analyze published chart-equivalent data transiently against independent SQL."""
import argparse
import collections
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time

import dashboard
import datalens_sdk
from datalens_sdk import DatasetDataFilter, DatasetDataSort, DataLensAPIError
from analytics_toolkit.datalens_utils.auth.client import datalens_client
from analytics_toolkit.datalens_utils.dashboard_generation.charts.create import chart_getter
from analytics_toolkit.datalens_utils.editing.state import configuration, inventory, metadata
from analytics_toolkit.datalens_utils.settings import read_chart_definitions, read_config

ROOT, RUNTIME_ROOT = dashboard.ROOT, dashboard.RUNTIME_ROOT
TEST_ROOT = ROOT / "sql_tests"
PARAMETER = re.compile(r"(\s*)(=|in)?(\s*)\{\{([^}]+)\}\}", re.IGNORECASE)
ESCAPES = {"\b": "\\b", "\f": "\\f", "\r": "\\r", "\n": "\\n", "\t": "\\t", "\0": "\\0", "\\": "\\\\", "'": "\\'"}
REL_TOL, ABS_TOL = 1e-9, 1e-4

class SqlReadError(RuntimeError):
    """A sanitized failure from the external toolkit process."""

def literal(value):
    return "'" + "".join(ESCAPES.get(character, character) for character in str(value)) + "'"

def bind_ql(query, definition, parameters):
    # Operator-aware tuple substitution follows the pinned official formatter:
    # datalens-ui/f581b7c31d6e9189ebeb1e1632b5fe7570534fb8/
    # src/server/modes/charts/plugins/ql/utils/misc-helpers.ts
    values = {}
    for parameter in definition["params"]:
        name, kind = parameter["name"], parameter["type"]
        value = parameters.get(name, parameter["default"])
        if kind == "date-interval":
            values.update({name + "_" + edge: ("date", value[edge]) for edge in ("from", "to")})
        else:
            values[name] = kind, value
    def replace(match):
        prefix, operator, spacing, name = match.groups()
        kind, value = values[name]
        items = value if isinstance(value, list) else [value]
        escaped = []
        for item in items:
            if kind == "number":
                assert math.isfinite(float(item)), "Non-finite QL parameter"
                escaped.append(str(float(item)))
            else:
                text = literal(str(item).lower() if isinstance(item, bool) else item)
                escaped.append("toDate(" + text + ")" if kind == "date" else text)
        result = ", ".join(escaped)
        if len(items) > 1 or (operator and operator.lower() == "in"):
            result = "(" + result + ")"
        return prefix + (operator or "") + spacing + result
    result = PARAMETER.sub(replace, query)
    assert "{{" not in result and "__TABLE" not in result, "Unresolved published QL SQL"
    return result

def compare_rows(actual, expected, profile):
    columns, numeric = profile["columns"], set(profile["numeric"])
    dimensions = [name for name in columns if name not in numeric]
    def value(row, name):
        item = row[name]
        if name == "Date":
            return str(item)[:10]
        if name == "Priority" and item not in ("Yes", "No"):
            return str(item).lower() in ("true", "1")
        return item
    def grouped(rows):
        result = collections.defaultdict(list)
        for row in rows:
            assert set(columns).issubset(row), "Returned aliases differ"
            result[tuple(value(row, name) for name in dimensions)].append(row)
        return result
    observed, wanted = grouped(actual), grouped(expected)
    assert observed.keys() == wanted.keys(), "Dimension identities differ"
    checked = 0
    for identity, rows in wanted.items():
        matches = observed[identity]
        assert len(matches) == len(rows), "Row multiplicities differ"
        def order(row):
            return tuple(float(row[name]) if row[name] is not None else -math.inf for name in profile["numeric"])
        for actual_row, expected_row in zip(sorted(matches, key=order), sorted(rows, key=order)):
            for name in numeric:
                left, right = actual_row[name], expected_row[name]
                assert (left is None and right is None) or (left is not None and right is not None
                    and math.isclose(float(left), float(right), rel_tol=REL_TOL, abs_tol=ABS_TOL)), "Numeric values differ in " + name
                checked += 1
    return checked

def with_selector_filters(query, conditions=()):
    """SQL files run as written; automated cases replace a comment anchor."""
    marker = "-- SELECTOR_FILTERS"
    assert not conditions or query.count(marker) == 1, "Selected SQL needs exactly one selector comment marker"
    return query.replace(marker, "\n".join("  AND " + condition for condition in conditions))

class SqlOracle:
    def __init__(self, python, db_key):
        self.python, self.db_key, self.cache = python, db_key, {}

    def read(self, query):
        if query not in self.cache:
            completed = subprocess.run([self.python, "-B", str(TEST_ROOT / "sql_read.py")],
                input=json.dumps([{"db_key": self.db_key, "query": query}]), capture_output=True, text=True, timeout=120)
            if completed.returncode != 0:
                raise SqlReadError("External analytics-toolkit SQL process failed")
            response = json.loads(completed.stdout)[0]
            if "error_type" in response:
                suffix = " (" + response["network_reason"] + ")" if response.get("network_reason") else ""
                raise SqlReadError("SQL read failed: " + response["error_type"] + suffix)
            self.cache[query] = response["rows"]
        return self.cache[query]

def editor_probe(key, chart, overrides=None, rows=None):
    result = subprocess.run(["node", str(TEST_ROOT / "editor_probe.mjs")], input=json.dumps({
        "key": key, "tabs": dict(chart.data), "overrides": overrides or {}, "rows": rows,
        "evaluate": rows is not None}), capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, "Published Editor script failed for " + key
    return json.loads(result.stdout)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analytics-python", default=sys.executable)
    parser.add_argument("--db-key", default="ch")
    args = parser.parse_args()
    skill = Path(datalens_sdk.agent_skill_paths()[0])
    preflight = subprocess.run(["bash", str(skill / "scripts/preflight.sh"), "yc"], cwd=ROOT,
        env={**os.environ, "PYTHON": sys.executable, "DATALENS_ORG_ID": dashboard.ORG_ID,
             "DATALENS_YC_BIN": str(dashboard.yc_binary()), "DATALENS_YC_PROFILE": dashboard.YC_PROFILE},
        capture_output=True, text=True)
    assert "---PREFLIGHT---" in preflight.stdout and "STATUS=ready" in preflight.stdout.splitlines(), "SDK preflight is not ready"
    project = dashboard.make_project()
    oracle = SqlOracle(args.analytics_python, args.db_key)
    suite = json.loads((TEST_ROOT / "cases.json").read_text())
    output = RUNTIME_ROOT / "sql-test-results.json"
    evidence = {"started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "kind": "Published dataset/QL/Editor code versus independent SQL; no browser execution",
        "tolerances": {"relative": REL_TOL, "absolute": ABS_TOL}, "cases": []}
    def save():
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(evidence, indent=2) + "\n")
    try:
        oracle.read("SELECT 1 AS connection_probe")
    except (SqlReadError, subprocess.TimeoutExpired) as error:
        evidence.update(status="blocked", planned_cases=len(suite["cases"]), evaluated_cases=0,
                        reason=str(error) if isinstance(error, SqlReadError) else "External SQL process timed out")
        save()
        raise SystemExit(evidence["reason"])
    project.verify()
    with project.session(), datalens_client() as client:
        definitions = read_chart_definitions()
        units = configuration()
        before = {key: metadata(entry) for key, entry in inventory(client, units).items()}
        dataset = client.get.dataset(by_id=read_config("DL objects/datasets.json")["retail"]["id"], branch="published")
        charts = {key: chart_getter(client, definitions[key])(by_id=definition["id"], branch="published") for key, definition in definitions.items()}
        cache = {}
        def query(request):
            assert request["datasetId"] == dataset.id, "Editor dataset binding differs"
            signature = json.dumps(request, sort_keys=True)
            if signature not in cache:
                fields = [dataset.fields.by_name(name) for name in request["columns"]]
                result = dataset.get_dataset_data(columns=fields,
                    filters=[DatasetDataFilter(dataset.fields.by_name(rule["column"]), rule["operation"], rule["values"]) for rule in request.get("where", [])],
                    sort=[DatasetDataSort(dataset.fields.by_name(rule["column"]), rule["direction"].lower()) for rule in request.get("order_by", [])], limit=request["limit"])
                assert len(result.rows) < request["limit"], "Dataset query reached its row limit"
                indexes = {column.guid: index for index, column in enumerate(result.schema)}
                cache[signature] = [{name: row[indexes[field.guid]] for name, field in zip(request["columns"], fields)} for row in result.rows]
            return cache[signature]
        selector = charts["js_selector"]
        options = editor_probe("js_selector", selector)["requests"]["selectorOptions"]
        rows = query(options)
        compare_rows(rows, oracle.read((TEST_ROOT / "selector_options.sql").read_text()), {"columns": ["Region"], "numeric": []})
        controls = editor_probe("js_selector", selector, rows={"selectorOptions": rows})["controls"]
        assert controls[0]["param"] == "region" and controls[0]["multiselect"], "Editor selector binding differs"
        evidence["selector_options"] = "passed"
        expected_defaults = {"wizard_revenue": read_config("UI/selectors/dataset/select_multi/wizard_region.json")["control"]["default_value"],
            "ql_revenue": [parameter["default"] for parameter in definitions["ql_revenue"]["params"] if parameter["name"] == "region"][0],
            "js_revenue": editor_probe("js_revenue", charts["js_revenue"])["params"]["region"]}
        for case in suite["cases"]:
            key, selected = case["chart"], case["regions"]
            definition, chart = definitions[key], charts[key]
            if case["key"].endswith("-default"):
                default = expected_defaults[key]
                assert selected == (default if isinstance(default, list) else [default]), "Default scenario differs from configuration"
            source = (TEST_ROOT / (key + ".sql")).read_text()
            assert "FROM " + read_config("DL objects/datasets.json")["retail"]["table"] in source, "Independent SQL table differs"
            filtered = [value for value in selected if value != "All"]
            conditions = ["region IN (" + ", ".join(literal(value) for value in filtered) + ")"] if filtered else []
            expected = oracle.read(with_selector_filters(source, conditions))
            if definition["family"] == "wizard":
                observed = query({"datasetId": dataset.id, "columns": ["Date", "Revenue"], "limit": 10000,
                    "where": [{"column": "Region", "operation": "IN", "values": filtered}] if filtered else [], "order_by": [{"column": "Date", "direction": "ASC"}]})
            elif definition["family"] == "ql":
                observed = oracle.read(bind_ql(chart.query_value, definition, {"region": selected}))
            else:
                probe = editor_probe(key, chart, {"region": selected})
                loaded = {name: query(request) for name, request in probe["requests"].items()}
                observed = editor_probe(key, chart, {"region": selected}, loaded)["values"]
            checked = compare_rows(observed, expected, {"columns": ["Date", "Revenue"], "numeric": ["Revenue"]})
            evidence["cases"].append({"key": case["key"], "status": "passed", "numeric_values_checked": checked})
            save()
        after = {key: metadata(entry) for key, entry in inventory(client, units).items()}
        assert before == after, "Resources changed during verification"
        evidence.update(status="passed", evaluated_cases=len(evidence["cases"]), revisions=after, dataset_queries=len(cache))
        save()
    print("Passed " + str(len(evidence["cases"])) + " cases; evidence: " + str(output))


if __name__ == "__main__":
    try:
        main()
    except DataLensAPIError as error:
        print("DataLens API failure: " + error.context.code + " request_id=" + str(error.context.request_id), file=sys.stderr)
        raise SystemExit(1)
