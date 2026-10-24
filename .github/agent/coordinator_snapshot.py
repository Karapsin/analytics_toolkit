"""Literal control schema, raw snapshots and coordinator eligibility."""
from __future__ import annotations

import copy
import math
import sqlite3
import time
from typing import Any

from . import coordinator_contract as contract

TABLES = {
    "coordinator_holds": ("repository", "number", "request", "binding"),
    "coordinator_renewals": ("repository", "number", "request", "binding", "head", "digest"),
    "pauses": ("repository", "number", "head", "reason", "created"),
    "vetoes": ("repository", "number", "head", "created"),
    "approvals": ("repository", "number", "head", "actor", "callback"),
    "feature_amendments": ("repository", "number", "head", "created"),
    "corrections": ("repository", "number", "head", "digest", "reason", "changes", "created"),
    "features": ("repository", "branch", "digest"),
    "feature_contexts": ("repository", "branch", "digest"),
}
RECEIPT_COLUMNS = ("repository", "number", "request", "binding_sha256", "transition_sha256",
                   "receipt_json", "receipt_sha256", "created")
RECEIPT_TABLE = "coordinator_checked_renewals"
PRIMARY = {
    "coordinator_holds": ("repository", "number", "request"),
    "coordinator_renewals": ("repository", "number", "request"),
    "pauses": ("repository", "number"), "vetoes": ("repository", "number", "head"),
    "approvals": ("repository", "number", "head"), "feature_amendments": ("repository", "number"),
    "corrections": ("repository", "number"), "features": ("repository", "branch"),
    "feature_contexts": ("repository", "branch", "digest"),
    RECEIPT_TABLE: ("repository", "number", "request", "binding_sha256"),
}


def deadline(end: float) -> None:
    if time.monotonic() >= end:
        contract.pending("budget_exceeded")


def finite_rows(cursor: sqlite3.Cursor, end: float) -> list[Any]:
    rows = []
    for row in cursor:
        deadline(end)
        if len(rows) >= contract.TABLE_ROWS:
            contract.pending("budget_exceeded")
        rows.append(row)
    return rows


def schema(db: sqlite3.Connection, end: float) -> None:
    """Reject executable schema side effects as well as incompatible columns."""
    names = (*TABLES, RECEIPT_TABLE)
    ordinary = {row[1]: row for row in finite_rows(db.execute("PRAGMA main.table_list"), end)
                if row[0] == "main"}
    for table in names:
        columns = TABLES.get(table, RECEIPT_COLUMNS)
        row = db.execute("SELECT type,rootpage FROM main.sqlite_master WHERE name=?", (table,)).fetchone()
        if (row is None or row[0] != "table" or row[1] <= 0 or table not in ordinary
                or ordinary[table][2:] != ("table", len(columns), 0, 0)):
            contract.pending("unsupported_schema")
        actual = finite_rows(db.execute(f'PRAGMA main.table_xinfo("{table}")'), end)
        expected = []
        primary = PRIMARY[table]
        for index, name in enumerate(columns):
            kind = "REAL" if name == "created" else "INTEGER" if name in {"number", "actor"} else "TEXT"
            pk = primary.index(name) + 1 if name in primary else 0
            expected.append((index, name, kind, int(table == RECEIPT_TABLE), None, pk, 0))
        if [tuple(row) for row in actual] != expected:
            contract.pending("unsupported_schema")
        if finite_rows(db.execute(f'PRAGMA main.foreign_key_list("{table}")'), end):
            contract.pending("unsupported_schema")
        for catalog in ("main.sqlite_master", "temp.sqlite_master"):
            if db.execute(f"SELECT 1 FROM {catalog} WHERE type='trigger' AND tbl_name=? LIMIT 1", (table,)).fetchone():
                contract.pending("unsupported_schema")
    unique_transition(db, end)


def unique_transition(db: sqlite3.Connection, end: float) -> None:
    for row in finite_rows(db.execute(f'PRAGMA main.index_list("{RECEIPT_TABLE}")'), end):
        if row[2] != 1 or row[4] != 0:
            continue
        # Index names originate in SQLite metadata, never public input.
        quoted = '"' + row[1].replace('"', '""') + '"'
        info = finite_rows(db.execute(f"PRAGMA main.index_xinfo({quoted})"), end)
        key = [tuple(item) for item in info if item[5] == 1]
        if len(key) == 1 and key[0][1:] == (4, "transition_sha256", 0, "BINARY", 1):
            return
    contract.pending("unsupported_schema")


def tagged(value: Any) -> list[Any]:
    if value is None:
        return ["null", None]
    if type(value) is str:
        return ["text", value]
    if type(value) is int:
        return ["integer", str(value)]
    if type(value) is float and math.isfinite(value):
        return ["real", value.hex()]
    contract.pending("authority_conflict")


def untagged(cell: Any) -> Any:
    contract.require(type(cell) is list and len(cell) == 2 and type(cell[0]) is str)
    tag, value = cell
    if tag == "null":
        contract.require(value is None)
        return None
    contract.require(type(value) is str)
    if tag == "text":
        return value
    if tag == "integer":
        contract.require(len(value) <= 20)
        result = int(value)
        contract.require(str(result) == value and -(2**63) <= result < 2**63)
        return result
    contract.require(tag == "real" and len(value) <= 32)
    number = float.fromhex(value)
    contract.require(math.isfinite(number) and number.hex() == value)
    return number


def row_bytes(row: list[Any]) -> bytes:
    return contract.bounded(row, contract.SNAPSHOT_BYTES, raw_strings=True)


def seal(scope: dict[str, Any], tables: list[dict[str, Any]]) -> dict[str, Any]:
    for table in tables:
        table["rows"].sort(key=row_bytes)
    body = {"version": 1, "repository": scope["repository"],
            "scope_sha256": contract.digest(scope), "tables": tables}
    return dict(body, sha256=contract.digest(body, raw_strings=True))


def selected(scope: dict[str, Any], table: str) -> tuple[str, list[Any]]:
    if table in {"features", "feature_contexts"}:
        values = sorted({p["branch"] for p in [scope["own"], *scope["peers"]]})
        column = "branch"
    else:
        values = [scope["own"]["number"], *(p["number"] for p in scope["peers"])]
        column = "number"
    return f"repository=? AND {column} IN ({','.join('?' for _ in values)})", [scope["repository"], *values]


def collect(db: sqlite3.Connection, scope: dict[str, Any], end: float) -> dict[str, Any]:
    tables, total, materialized = [], 0, 0
    for name, columns in TABLES.items():
        deadline(end)
        where, args = selected(scope, name)
        metadata = ",".join(f"typeof({c}),length(CAST({c} AS BLOB))" for c in columns)
        lengths = finite_rows(db.execute(f"SELECT {metadata} FROM {name} WHERE {where} LIMIT 4097", args), end)
        total += len(lengths)
        if total > contract.MAX_ROWS:
            contract.pending("budget_exceeded")
        for row in lengths:
            if any(row[i] not in {"null", "text", "integer", "real"} for i in range(0, len(row), 2)):
                contract.pending("authority_conflict")
            length = sum(row[i] or 0 for i in range(1, len(row), 2))
            materialized += length
            if length > contract.ROW_BYTES or materialized > contract.SNAPSHOT_BYTES:
                contract.pending("budget_exceeded")
        rows = []
        cursor = db.execute(f"SELECT {','.join(columns)} FROM {name} WHERE {where} LIMIT 4097", args)
        for row in finite_rows(cursor, end):
            rows.append([tagged(cell) for cell in row])
        tables.append({"name": name, "columns": list(columns), "rows": rows})
    return seal(scope, tables)


def validate(value: Any, scope: dict[str, Any]) -> None:
    contract.bounded(value, contract.SNAPSHOT_BYTES, raw_strings=True)
    contract.keys(value, ("version", "repository", "scope_sha256", "tables", "sha256"))
    contract.integer(value["version"], 1, 1)
    contract.require(value["repository"] == scope["repository"] and value["scope_sha256"] == contract.digest(scope))
    contract.require(type(value["tables"]) is list and len(value["tables"]) == len(TABLES))
    count = 0
    for item, (name, columns) in zip(value["tables"], TABLES.items()):
        contract.keys(item, ("name", "columns", "rows"))
        contract.require(item["name"] == name and item["columns"] == list(columns))
        contract.require(type(item["rows"]) is list and len(item["rows"]) <= contract.TABLE_ROWS)
        count += len(item["rows"])
        contract.require(count <= contract.MAX_ROWS)
        for row in item["rows"]:
            contract.require(type(row) is list and len(row) == len(columns))
            decoded = [untagged(cell) for cell in row]
            contract.require(sum(len(str(v).encode("utf-8")) for v in decoded if v is not None) <= contract.ROW_BYTES)
            record = dict(zip(columns, decoded))
            contract.require(record["repository"] == scope["repository"])
            column = "branch" if name in {"features", "feature_contexts"} else "number"
            allowed = {p[column] for p in [scope["own"], *scope["peers"]]}
            contract.require(record[column] in allowed)
        contract.require(item["rows"] == sorted(item["rows"], key=row_bytes))
    contract.require(value == seal(scope, copy.deepcopy(value["tables"])))


def records(value: dict[str, Any], table: str, number: int) -> list[dict[str, Any]]:
    item = value["tables"][list(TABLES).index(table)]
    result = [dict(zip(item["columns"], (untagged(cell) for cell in row))) for row in item["rows"]]
    return [row for row in result if row.get("number") == number]


def live_identity(row: dict[str, Any], target: dict[str, Any]) -> bool:
    try:
        contract.hex_value(row["head"], 40)
        if "digest" in row:
            contract.hex_value(row["digest"])
    except ValueError:
        contract.pending("authority_conflict")
    if row["head"] == target["head"] and "digest" in row and row["digest"] != target["task_digest"]:
        contract.pending("authority_conflict")
    return row["head"] == target["head"]


def renewal_identity(row: dict[str, Any]) -> None:
    try:
        contract.identifier(row["request"])
        contract.require(type(row["binding"]) is str and len(row["binding"].encode()) <= 16384)
        binding = contract.decode(row["binding"])
        contract.require(type(binding) is dict and binding.get("repository") == row["repository"]
                         and binding.get("request_id") == row["request"])
        contract.integer(binding.get("revision"))
        contract.bounded(binding, 16384)
    except (ValueError, TypeError):
        contract.pending("authority_conflict")


def eligibility(value: dict[str, Any], scope: dict[str, Any], *, replay: bool = False) -> None:
    own, binding = scope["own"], scope["binding"]
    held = records(value, "coordinator_holds", own["number"])
    expected = {"repository": scope["repository"], "number": own["number"],
                "request": binding["request_id"], "binding": contract.legacy(binding)}
    if held != ([] if replay else [expected]):
        contract.pending("stale_replay" if replay else "stale_hold")
    for target in [own, *scope["peers"]]:
        number, is_own = target["number"], target == own
        if any(records(value, name, number) for name in ("pauses", "feature_amendments")):
            contract.pending("authority_conflict")
        if not is_own and records(value, "coordinator_holds", number):
            contract.pending("authority_conflict")
        if any(live_identity(row, target) for row in records(value, "vetoes", number)):
            contract.pending("authority_conflict")
        for name in ("corrections", "coordinator_renewals"):
            for row in records(value, name, number):
                if name == "coordinator_renewals":
                    renewal_identity(row)
                if live_identity(row, target) and not is_own:
                    contract.pending("authority_conflict")


def own_renewal(value: dict[str, Any], scope: dict[str, Any]) -> dict[str, Any] | None:
    binding = scope["binding"]
    rows = [r for r in records(value, "coordinator_renewals", scope["own"]["number"])
            if r["request"] == binding["request_id"]]
    if len(rows) > 1:
        contract.pending("authority_conflict")
    return rows[0] if rows else None


def post_state(value: dict[str, Any], scope: dict[str, Any]) -> dict[str, Any]:
    eligibility(value, scope)
    prior = own_renewal(value, scope)
    binding = scope["binding"]
    encoded = contract.legacy(binding)
    if prior:
        if prior["binding"] == encoded:
            contract.pending("legacy_outcome")
        try:
            old = contract.decode(prior["binding"])
            contract.require(type(old) is dict and old.get("request_id") == binding["request_id"]
                             and old.get("repository") == scope["repository"])
            contract.integer(old.get("revision"))
            contract.require(old["revision"] < binding["revision"])
        except (ValueError, TypeError):
            contract.pending("authority_conflict")
    tables = copy.deepcopy(value["tables"])
    own = scope["own"]
    for item in tables:
        if item["name"] == "coordinator_holds":
            item["rows"] = [row for row in item["rows"] if untagged(row[1]) != own["number"]]
        if item["name"] == "coordinator_renewals":
            item["rows"] = [row for row in item["rows"] if not (untagged(row[1]) == own["number"]
                            and untagged(row[2]) == binding["request_id"])]
            item["rows"].append([tagged(v) for v in (scope["repository"], own["number"],
                binding["request_id"], encoded, own["head"], own["task_digest"])])
    return seal(scope, tables)
