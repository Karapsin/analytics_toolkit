#!/usr/bin/env python3
"""Copy a portable recipe without deploying, installing, or overwriting files."""

import argparse
import json
from pathlib import Path
import re
import shutil


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--table", required=True)
    parser.add_argument("--organization-id", default="")
    parser.add_argument("--yc-profile", default="")
    parser.add_argument("--target-path", default="")
    parser.add_argument("--dashboard-name", default="Sales dashboard")
    parser.add_argument("--connection-name", default="")
    parser.add_argument("--connection-id", default="")
    parser.add_argument("--schema-version", type=int, choices=(1, 2), default=1)
    parser.add_argument("--installation", choices=("yc", "enterprise"), default="yc")
    parser.add_argument("--base-url")
    parser.add_argument("--token-env")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--workbook-id")
    target.add_argument("--workbook-key")
    parser.add_argument("--connector", default="clickhouse")
    parser.add_argument("--source-factory")
    parser.add_argument(
        "--source-parameters", default="{}", help="Public factory parameters as JSON."
    )
    arguments = parser.parse_args()
    if arguments.schema_version == 1 and (
        arguments.installation != "yc"
        or arguments.workbook_id
        or arguments.workbook_key
        or arguments.connector != "clickhouse"
        or arguments.source_factory
    ):
        parser.error("installation, workbook and connector options require --schema-version 2")
    if arguments.installation == "enterprise" and not arguments.base_url:
        parser.error("Enterprise requires --base-url")
    if (
        arguments.schema_version == 2
        and arguments.connector != "clickhouse"
        and not arguments.source_factory
    ):
        parser.error("Additional connectors require an explicit --source-factory")
    try:
        source_parameters = json.loads(arguments.source_parameters)
    except ValueError:
        parser.error("--source-parameters must be JSON")
    if not isinstance(source_parameters, dict):
        parser.error("--source-parameters must be a JSON object")
    destination = arguments.destination.expanduser().resolve()
    if destination.exists():
        parser.error("destination already exists; choose a new project directory")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*", arguments.table):
        parser.error("table must be an ordinary database.table identifier")
    replacements = {"@TABLE@": arguments.table}
    for name in (
        "organization_id",
        "yc_profile",
        "target_path",
        "dashboard_name",
        "connection_name",
        "connection_id",
    ):
        replacements["@" + name.upper() + "@"] = json.dumps(
            getattr(arguments, name), ensure_ascii=False
        )
    assets = Path(__file__).resolve().parents[1] / "assets/project"
    shutil.copytree(assets, destination)
    for path in destination.rglob("*"):
        if path.is_file():
            value = path.read_text(encoding="utf-8")
            for token, replacement in replacements.items():
                value = value.replace(token, replacement)
            path.write_text(value, encoding="utf-8")
    if arguments.schema_version == 2:
        configure_bi(destination, arguments, source_parameters)
    print("Created " + str(destination))
    print(
        "Check the source field mapping and manual options, then run python dashboard.py validate in your existing analytics-toolkit environment."
    )


def configure_bi(destination, arguments, parameters):
    runtime = destination / "configs/runtime.json"
    runtime.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "installation": arguments.installation,
                "request_interval_seconds": 1.2,
            },
            indent=2,
        )
        + "\n"
    )
    objects = destination / "configs/DL objects"
    (objects / "connections.json").write_text(
        json.dumps(
            {
                "default": {
                    "mode": "existing",
                    "name": arguments.connection_name,
                    "id": arguments.connection_id,
                    "connector": arguments.connector,
                }
            },
            indent=2,
        )
        + "\n"
    )
    path = objects / "datasets.json"
    datasets = json.loads(path.read_text())
    tables = {key: value["table"] for key, value in datasets.items()}
    factory = arguments.source_factory or "ch_subselect"
    for definition in datasets.values():
        source = {"connection": "default", "factory": factory, "parameters": parameters}
        if not arguments.source_factory:
            source["sql_file"] = definition["projection_file"]
        definition["sources"] = {"facts": source}
        definition.pop("projection_file", None)
        definition.pop("table", None)
        definition.pop("source", None)
        for column, field in definition["fields"].items():
            field.update(source="facts", column=column)
    path.write_text(json.dumps(datasets, indent=2) + "\n")
    for path in (destination / "assets/sql").rglob("*.sql"):
        content = path.read_text()
        for key, table in tables.items():
            content = content.replace("__TABLE_" + key.upper() + "__", table)
        path.write_text(content)
    chart_paths = [
        path
        for directory in ("charts", "selectors")
        for path in (objects / directory).rglob("*.json")
    ]
    for path in chart_paths:
        definition = json.loads(path.read_text())
        # These legacy coverage declarations are not SDK recipe operations.
        definition.pop("series_type", None)
        definition.pop("parameter_bindings", None)
        definition.pop("control_types", None)
        path.write_text(json.dumps(definition, indent=2) + "\n")
    if arguments.workbook_key:
        (objects / "workbooks.json").write_text(
            json.dumps(
                {arguments.workbook_key: {"name": arguments.dashboard_name + " workbook"}}, indent=2
            )
            + "\n"
        )
    launcher = destination / "dashboard.py"
    text = launcher.read_text().replace(
        "DataLensProject, Deployment",
        "BIProjectDeployment, DataLensProject, Deployment, TargetLocation",
    )
    start = text.index("    deployment = Deployment(")
    end = text.index("    return DataLensProject", start)
    if arguments.workbook_id:
        target = "TargetLocation.workbook(by_id=" + repr(arguments.workbook_id) + ")"
    elif arguments.workbook_key:
        target = "TargetLocation.workbook(key=" + repr(arguments.workbook_key) + ")"
    else:
        target = "TargetLocation.path(TARGET_PATH)"
    text = (
        text[:start]
        + "    deployment = BIProjectDeployment(DASHBOARD_NAME, "
        + target
        + ",\n"
        + "        installation="
        + repr(arguments.installation)
        + ", organization_id=ORG_ID, yc_profile=YC_PROFILE,\n"
        + "        yc_binary=yc_binary(), base_url="
        + repr(arguments.base_url)
        + ", token_env="
        + repr(arguments.token_env)
        + ")\n"
        + text[end:]
    )
    launcher.write_text(text)


if __name__ == "__main__":
    main()
