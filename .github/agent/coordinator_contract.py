"""Closed, bounded wire contracts for checked coordinator renewal."""
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

OPERATION = "checked-coordinator-renewal-v1"
SCOPE_BYTES = 4 * 1024 * 1024
RECEIPT_BYTES = 3 * 1024 * 1024
SNAPSHOT_BYTES = SCOPE_BYTES
MAX_ROWS = 8192
TABLE_ROWS = 4096
ROW_BYTES = 262144
MAX_NODES = 200000
MAX_DEPTH = 24
STRING_BYTES = 65536
CALLER_KEYS = frozenset((
    "version", "operation", "repository", "number", "request_id", "hold_revision",
    "request_revision", "request_status", "request_stage", "generation",
    "request_row_sha256", "candidate_head", "task_digest", "dev_base", "main_head",
    "plan_receipt_sha256", "scope_sha256", "proof_sha256", "checkpoint_sha256",
    "patch_sha256", "worker_source_sha256", "controller_source_sha256",
    "runtime_sha256", "global_settings_sha256", "external_observation_sha256",
))


class CoordinatorAuthorityPending(RuntimeError):
    """A bounded operational refusal, never owner permission or an empty scope."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def pending(code: str) -> None:
    raise CoordinatorAuthorityPending(code)


def require(condition: bool) -> None:
    if not condition:
        raise ValueError("Invalid checked coordinator contract")


def keys(value: Any, expected: Any) -> None:
    require(type(value) is dict and all(type(k) is str for k in value)
            and set(value) == set(expected))


def integer(value: Any, maximum: int = 9007199254740991, minimum: int = 0) -> None:
    require(type(value) is int and minimum <= value <= maximum)


def hex_value(value: Any, length: int = 64) -> None:
    require(type(value) is str and re.fullmatch("[0-9a-f]{" + str(length) + "}", value) is not None)


def text(value: Any, maximum: int = 12000) -> None:
    require(type(value) is str and bool(value.strip()) and len(value) <= maximum)


def identifier(value: Any) -> None:
    require(type(value) is str and re.fullmatch(r"[A-Za-z0-9:_-]{1,128}", value) is not None)


def member(value: Any, allowed: set[str]) -> None:
    require(type(value) is str and value in allowed)


def repository(value: Any) -> None:
    require(type(value) is str and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", value) is not None)
    require(all(part not in {".", ".."} for part in value.split("/")))


def branch(value: Any) -> None:
    require(type(value) is str and re.fullmatch(r"[A-Za-z0-9_./-]{1,200}", value) is not None)
    require(all(part not in {"", ".", ".."} for part in value.split("/")))


def bounded(value: Any, maximum: int, *, raw_strings: bool = False) -> bytes:
    """Check types, cycles and traversal budgets before bounded JSON encoding."""
    stack = [(value, 0, False)]
    active: set[int] = set()
    nodes = size = 0
    while stack:
        item, depth, leaving = stack.pop()
        if leaving:
            active.remove(id(item))
            continue
        nodes += 1
        if nodes > MAX_NODES or depth > MAX_DEPTH:
            pending("budget_exceeded")
        kind = type(item)
        require(kind in {dict, list, str, int, bool, float, type(None)})
        if kind in {dict, list}:
            require(id(item) not in active)
            active.add(id(item))
            stack.append((item, depth, True))
            if len(item) > MAX_NODES - nodes:
                pending("budget_exceeded")
            children = list(item.items()) if kind is dict else enumerate(item)
            for key, child in children:
                if kind is dict:
                    require(type(key) is str)
                    stack.append((key, depth + 1, False))
                stack.append((child, depth + 1, False))
        elif kind is str:
            # A character bound prevents allocating an unbounded UTF8 buffer.
            limit = ROW_BYTES if raw_strings else STRING_BYTES
            if len(item) > limit:
                pending("budget_exceeded")
            length = len(item.encode("utf-8"))
            if length > limit:
                pending("budget_exceeded")
            size += length
        elif kind is float:
            require(math.isfinite(item))
            size += len(repr(item))
        elif kind is int:
            if item.bit_length() > maximum * 4:
                pending("budget_exceeded")
            size += max(1, (item.bit_length() - 1) * 3010 // 10000 + 1) + int(item < 0)
        else:
            size += 5 if item is False else 4
        if size > maximum:
            pending("budget_exceeded")
    chunks = []
    size = 0
    encoder = json.JSONEncoder(sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    for chunk in encoder.iterencode(value):
        encoded = chunk.encode("utf-8")
        size += len(encoded)
        if size > maximum:
            pending("budget_exceeded")
        chunks.append(encoded)
    return b"".join(chunks)


def digest(value: Any, maximum: int = SCOPE_BYTES, *, raw_strings: bool = False) -> str:
    return hashlib.sha256(bounded(value, maximum, raw_strings=raw_strings)).hexdigest()


def clone(value: Any, maximum: int, *, raw_strings: bool = False) -> Any:
    """Use a private wire value after validation, never a mutable caller object."""
    return decode(bounded(value, maximum, raw_strings=raw_strings).decode())


def legacy(binding: dict[str, Any]) -> str:
    bounded(binding, 16384)
    result = json.dumps(binding, sort_keys=True)
    if len(result.encode("utf-8")) > 16384:
        pending("budget_exceeded")
    return result


def decode(value: str) -> Any:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        require(len({key for key, _ in pairs}) == len(pairs))
        return dict(pairs)

    try:
        return json.loads(value, object_pairs_hook=unique, parse_constant=lambda _: require(False))
    except RecursionError:
        pending("budget_exceeded")


def target(value: Any) -> None:
    keys(value, ("number", "head", "task_digest", "branch"))
    integer(value["number"], 2147483647, 1)
    hex_value(value["head"], 40)
    hex_value(value["task_digest"])
    branch(value["branch"])


def hold(value: Any, scope: dict[str, Any]) -> None:
    required = {"request_id", "revision", "repository", "stage", "dev_base", "state_sha256",
                "checkpoint_sha256", "number", "candidate_head", "task_digest", "kind",
                "prerequisite_review_digest", "plan_binding_digest"}
    require(type(value) is dict)
    keys(value, required | ({"coordinator_predecessor_sha256"} if "coordinator_predecessor_sha256" in value else set()))
    identifier(value["request_id"])
    integer(value["revision"])
    require(value["repository"] == scope["repository"] and value["number"] == scope["own"]["number"])
    integer(value["number"], 2147483647, 1)
    require(value["candidate_head"] == scope["own"]["head"] and value["task_digest"] == scope["own"]["task_digest"])
    member(value["stage"], {"plan", "plan-review", "plan-resolver", "code", "publish", "prerequisite-check"})
    require(value["kind"] == "pr_dev_merge")
    hex_value(value["dev_base"], 40)
    for key in ("state_sha256", "checkpoint_sha256", "prerequisite_review_digest", "plan_binding_digest"):
        hex_value(value[key])
    if "coordinator_predecessor_sha256" in value:
        hex_value(value["coordinator_predecessor_sha256"])
    legacy(value)


def review_receipt(scope: dict[str, Any]) -> None:
    value, binding = scope["review_receipt"], scope["binding"]
    bounded(value, RECEIPT_BYTES)
    keys(value, ("version", "binding", "prerequisites", "profile_sources", "reassessments", "review_revision", "execution", "sha256"))
    integer(value["version"], 2, 2)
    hex_value(value["sha256"])
    require(digest({k: v for k, v in value.items() if k != "sha256"}) == value["sha256"])
    require(digest(value) == binding["prerequisite_review_digest"])
    plan = value["binding"]
    keys(plan, ("request_id", "head", "base", "context_digest", "plan_sha256"))
    require(plan["request_id"] == binding["request_id"] and plan["base"] == binding["dev_base"])
    hex_value(plan["head"], 40)
    hex_value(plan["context_digest"])
    hex_value(plan["plan_sha256"])
    require(digest(plan) == binding["plan_binding_digest"])
    selected = review_items(value["prerequisites"], scope)
    require([(p["number"], p["branch"]) for p in scope["peers"]] == sorted(selected))
    profiles = value["profile_sources"]
    capture = any(p["kind"] == "capture_isolation_active_v1" for p in value["prerequisites"])
    keys(profiles, ("capture_isolation_active_v1",) if capture else ())
    if capture:
        require(type(profiles["capture_isolation_active_v1"]) is dict and bool(profiles["capture_isolation_active_v1"]))
    execution(value["execution"], plan)
    integer(value["review_revision"])
    require(type(value["reassessments"]) is list and len(value["reassessments"]) <= 16)
    numbers = set()
    for item in value["reassessments"]:
        keys(item, ("number", "reason", "amendment_sha256"))
        integer(item["number"], 2147483647, 1)
        require(item["number"] not in numbers)
        numbers.add(item["number"])
        text(item["reason"])
        hex_value(item["amendment_sha256"])


def review_items(items: Any, scope: dict[str, Any]) -> list[tuple[int, str]]:
    require(type(items) is list and 1 <= len(items) <= 16)
    selected = []
    for item in items:
        keys(item, ("kind", "repository", "number", "target", "task_id", "branch", "anchor_head",
                    "anchor_task_digest", "required_before", "reason", "plan_location"))
        member(item["kind"], {"pr_dev_merge", "capture_isolation_active_v1"})
        require(item["repository"] == scope["repository"] and item["target"] == "dev")
        integer(item["number"], 2147483647, 1)
        require(item["number"] != scope["own"]["number"] and item["number"] not in {p[0] for p in selected})
        identifier(item["task_id"])
        branch(item["branch"])
        hex_value(item["anchor_head"], 40)
        hex_value(item["anchor_task_digest"])
        require(item["required_before"] == ["code", "publication"])
        text(item["reason"])
        text(item["plan_location"])
        selected.append((item["number"], item["branch"]))
    return selected


def execution(value: Any, plan: dict[str, Any]) -> None:
    keys(value, ("id", "identity", "thread_id", "events_sha256", "result_sha256"))
    hex_value(value["id"], 32)
    for name in ("events_sha256", "result_sha256"):
        hex_value(value[name])
    text(value["thread_id"])
    identity = value["identity"]
    require(type(identity) is dict and identity.get("id") == value["id"]
            and identity.get("request_id") == plan["request_id"] and identity.get("plan_binding") == plan)
    text(identity.get("planner_thread_id"))
    require(value["thread_id"] != identity["planner_thread_id"])


def validate_scope(repo: str, value: Any) -> None:
    bounded(value, SCOPE_BYTES)
    keys(value, ("version", "repository", "own", "binding", "review_receipt", "peers"))
    integer(value["version"], 1, 1)
    repository(repo)
    require(value["repository"] == repo)
    target(value["own"])
    require(type(value["peers"]) is list and 1 <= len(value["peers"]) <= 16)
    for peer in value["peers"]:
        target(peer)
    require([p["number"] for p in value["peers"]] == sorted({p["number"] for p in value["peers"]}))
    hold(value["binding"], value)
    review_receipt(value)


def validate_caller(scope: dict[str, Any], proof: str, caller: Any) -> None:
    hex_value(proof)
    bounded(caller, 8192)
    keys(caller, CALLER_KEYS)
    integer(caller["version"], 1, 1)
    require(caller["operation"] == OPERATION)
    binding, own = scope["binding"], scope["own"]
    expected = {"repository": scope["repository"], "number": own["number"],
                "request_id": binding["request_id"], "hold_revision": binding["revision"],
                "candidate_head": own["head"], "task_digest": own["task_digest"],
                "dev_base": binding["dev_base"], "scope_sha256": digest(scope),
                "proof_sha256": proof, "checkpoint_sha256": binding["checkpoint_sha256"],
                "plan_receipt_sha256": binding["prerequisite_review_digest"]}
    require(all(caller[k] == v and type(caller[k]) is type(v) for k, v in expected.items()))
    integer(caller["request_revision"], minimum=binding["revision"])
    member(caller["request_status"], {"waiting", "running"})
    require(caller["request_stage"] == "prerequisite-check")
    hex_value(caller["generation"], 32)
    hex_value(caller["main_head"], 40)
    hex_value(proof)
    for key in CALLER_KEYS:
        if key.endswith("_sha256"):
            hex_value(caller[key])
