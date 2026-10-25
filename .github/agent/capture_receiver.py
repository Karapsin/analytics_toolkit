"""Purpose-separated source-pinned receiver. No positive host Gate is issued.

Only trusted runner entrypoints execute the fixed synthetic sentinel; production
code stays inside the independently admitted isolation helper. Host publication
remains pending until its separately implemented live authority boundary exists.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import importlib.util
import os
import re
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

_data_path = Path(__file__).with_name("capture_data.py").resolve()
_data_key = "atk_capture_source:" + str(_data_path)
_spec = importlib.util.spec_from_file_location(_data_key, _data_path)
if _spec is None or _spec.loader is None:
    raise RuntimeError("Trusted capture metadata unavailable")
D = sys.modules.get(_data_key)
if D is None:
    D = importlib.util.module_from_spec(_spec)
    sys.modules[_data_key] = D
    _spec.loader.exec_module(D)
elif Path(D.__file__).resolve() != _data_path:
    raise RuntimeError("Trusted capture metadata identity changed")
T = D.sibling("capture_transport")
PROTOCOL = "capture-receiver-v2"
# Approved globalV3 actual_positive_wire/closed_native_parser.image_contract.
# Exact3.11 image membership is a negative guard, not fresh native acceptance.
NATIVE_IMAGE_311 = "sha256:bf91cbbb4f6b3105745c0526dc4bfc7226d8bd39e5df5f59882c8bca3f787b8f"
FIXTURE = ".github/agent/fixtures/capture_receiver_sentinel_v1/"
SOURCE_PATHS = (
    ".github/agent/controller.py",
    ".github/agent/dispatch.py",
    ".github/agent/capture_data.py",
    ".github/agent/capture_transport.py",
    ".github/agent/capture_receiver.py",
    ".github/agent/capture_receiver.schema.json",
    ".github/agent/capture_isolation.py",
    ".github/agent/capture_sandbox.py",
    ".github/agent/capture_artifact.py",
    "agent_tools/sql_explorer_visual_render.py",
    FIXTURE + "manifest.json",
    FIXTURE + "sentinel.py",
    ".github/workflows/agent-dispatch.yml",
    T.WORKFLOW,
)


def schema():
    # This literal file is exactly the approved globalV3 normative schema.
    path = Path(__file__).with_name("capture_receiver.schema.json")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size != 20058:
            raise D.Pending("Normative receiver schema unavailable")
        raw = os.read(fd, 65536)
        if (
            D.identity(before) != D.identity(os.fstat(fd))
            or D.identity(before) != D.identity(os.stat(path, follow_symlinks=False))
            or D.digest(raw) != "7931d1a5044d2ae3149ab9ba5bf75e18ac6ca08fbc1d24a9aba70b279339b7cf"
        ):
            raise D.Pending("Normative receiver schema changed")
    finally:
        os.close(fd)
    return D.parse(raw, 65536)


def protocol():
    root = Path(__file__).resolve().parents[2]
    if any(not (root / path).is_file() or (root / path).is_symlink() for path in SOURCE_PATHS):
        raise D.Pending("Composed trusted capture helper/source closure unavailable")
    schema()
    return {
        "protocol": PROTOCOL,
        "schema_version": 2,
        "operations": dict(T.OPERATIONS),
        "workflow_path": T.WORKFLOW,
        "repository": T.REPOSITORY,
        "source_paths": list(SOURCE_PATHS),
    }


def validate_type(value, name):
    contract = schema()
    if not D.matches(value, contract["$defs"][name], contract["$defs"]):
        raise ValueError("Invalid closed capture " + name)
    return value


def validate_envelope(raw_bytes, *, expected_purpose, now):
    envelope = D.parse(raw_bytes, 4096)
    definition = {
        "production": "productionEnvelope",
        "receiver-rehearsal": "rehearsalEnvelope",
    }.get(expected_purpose)
    if definition is None:
        raise ValueError("Unsupported capture purpose")
    validate_type(envelope, definition)
    if raw_bytes != D.canonical(envelope):
        raise ValueError("Noncanonical capture envelope")
    source, attempt, authority = (envelope[k] for k in ("source", "attempt", "authorization"))
    if source["source_tag"] != "agent-visual-source-v1-" + source["expected_trusted_head"]:
        raise ValueError("Capture source tag mismatch")
    if not D.integer(now) or not (
        attempt["created_at"] <= now <= attempt["expires_at"]
        and 1 <= attempt["expires_at"] - attempt["created_at"] <= 300
    ):
        raise ValueError("Expired capture envelope")
    if (
        expected_purpose == "receiver-rehearsal"
        and authority["fixture_source_commit"] != source["expected_trusted_head"]
    ):
        raise ValueError("Rehearsal fixture source mismatch")
    return envelope


def binding_bytes(envelope):
    core = copy.deepcopy(envelope)
    del core["authorization"]["admission_receipt_sha256"]
    return D.canonical(core)


def monitor_body(envelope):
    raw = D.canonical({"operation": envelope["operation"], "value": envelope})
    body = b"<!-- github-agent-dispatch\n" + raw + b"\n-->"
    if len(body) > 4096:
        raise ValueError("Capture monitor body limit")
    return body.decode("utf-8")


def authenticate_comment(event, envelope):
    """Actual API/event-owned identities, not names asserted in comment text."""
    issue, comment, sender = (event[k] for k in ("issue", "comment", "sender"))
    if (
        event.get("repository", {}).get("full_name") != T.REPOSITORY
        or issue.get("title") != "GitHub agent integration monitor"
        or issue.get("state") != "open"
        or not isinstance(issue.get("labels"), list)
        or not any(
            isinstance(label, dict) and label.get("name") == "agent:state"
            for label in issue.get("labels", [])
        )
        or "pull_request" in issue
        or not D.integer(issue.get("number"))
        or not D.integer(issue.get("id"))
        or issue.get("url")
        != "https://api.github.com/repos/" + T.REPOSITORY + "/issues/" + str(issue["number"])
        or sender.get("id") != 340139075
        or sender.get("type") != "Bot"
        or sender.get("login") != "analytics-toolkit-agent-717959[bot]"
    ):
        raise ValueError("Unauthenticated capture monitor")
    binding = {
        "monitor_issue": issue["number"],
        "comment_body_sha256": D.digest(monitor_body(envelope).encode()),
    }
    if not T.comment_identity(comment, binding):
        raise ValueError("Unauthenticated capture App comment")
    return comment["id"]


def read_host_admission(state_root, admission_ref, envelope, *, now=None, budget=None):
    """Read/bind an original only. This never establishes live caller authority."""
    budget = D.Budget() if budget is None else budget
    now = int(time.time()) if now is None else now
    auth = envelope["authorization"]
    purpose, nonce = envelope["purpose"], envelope["attempt"]["nonce"]
    if purpose == "production":
        fields = {"schema_version", "purpose", "request_id", "nonce", "receipt_sha256"}
        parts = ("requests", auth["request_id"], "capture-admissions", nonce, "admission.json")
        comparisons = ("request_id", "revision", "generation", "gate_proof_sha256")
        receipt_type = "productionAdmissionReceipt"
        id_key, ident = "request_id", auth["request_id"]
    elif purpose == "receiver-rehearsal":
        fields = {"schema_version", "purpose", "operation_id", "case_id", "nonce", "receipt_sha256"}
        parts = (
            "source-deployments",
            auth["operation_id"],
            "admissions",
            auth["case_id"],
            nonce + ".json",
        )
        comparisons = (
            "operation_id",
            "operation_revision",
            "operation_generation",
            "operation_receipt_sha256",
            "case_id",
            "recipe_sha256",
            "fixture_manifest_sha256",
        )
        receipt_type = "rehearsalAdmissionReceipt"
        id_key, ident = "operation_id", auth["operation_id"]
    else:
        raise ValueError("Unsupported admission purpose")
    if (
        not isinstance(admission_ref, dict)
        or set(admission_ref) != fields
        or type(admission_ref["schema_version"]) is not int
        or admission_ref["schema_version"] != 1
        or admission_ref["purpose"] != purpose
        or admission_ref["nonce"] != nonce
        or admission_ref[id_key] != ident
        or admission_ref["receipt_sha256"] != auth["admission_receipt_sha256"]
        or purpose == "receiver-rehearsal"
        and admission_ref["case_id"] != auth["case_id"]
    ):
        raise ValueError("Invalid capture private admission reference")
    try:
        with D.Store(state_root, budget=budget) as store:
            raw = store.read(parts, 65536)
    except FileNotFoundError:
        raise D.Pending("Genuine purpose-specific admission not issued") from None
    receipt = validate_type(D.parse(raw, 65536), receipt_type)
    if (
        raw != D.canonical(receipt)
        or D.digest(raw) != admission_ref["receipt_sha256"]
        or receipt["envelope_binding_sha256"] != D.digest(binding_bytes(envelope))
        or any(receipt[key] != auth[key] for key in comparisons)
        or not receipt["issued_at"] <= now <= receipt["expires_at"]
        or receipt["expires_at"] != envelope["attempt"]["expires_at"]
        or not 1 <= receipt["expires_at"] - receipt["issued_at"] <= 300
    ):
        raise ValueError("Stale or mismatched capture admission original")
    return receipt


def require_live_host_boundary(purpose):
    """Explicit capability absence, not a user-written permit/file/boolean.

    The positive worker and real source-operation issuer are separate source
    units. Until that supported current-row/operation CAS exists, publication
    cannot be reached from these host wrappers, even with schema-valid files.
    """
    if purpose not in T.OPERATIONS and purpose != "source-deployment":
        raise ValueError("Invalid live capture boundary purpose")
    raise D.Pending(
        "Live capture " + purpose + " issuer/current-authority boundary is not implemented"
    )


def dispatch_rehearsal_request(gh, operation_ref, case_id, admission_ref):
    if gh.repository != T.REPOSITORY or not isinstance(operation_ref, dict):
        raise ValueError("Unsupported rehearsal source operation")
    # No PR/candidate/patch/command/ref input; real operation+case reader and
    # live cancellation/current-source CAS must arrive in its reviewed unit.
    if set(operation_ref) != {"operation_id", "receipt_sha256"}:
        raise ValueError("Invalid rehearsal operation reference")
    if case_id not in schema()["$defs"]["caseId"]["enum"] or not isinstance(admission_ref, dict):
        raise ValueError("Invalid rehearsal case reference")
    require_live_host_boundary("receiver-rehearsal")


def create_source_pin(gh, expected_main, composed_profile, deployment_intent):
    if gh.repository != T.REPOSITORY or re.fullmatch("[0-9a-f]{40}", expected_main) is None:
        raise ValueError("Invalid source publication repository/head")
    if not isinstance(composed_profile, dict) or not isinstance(deployment_intent, dict):
        raise ValueError("Missing source publication originals")
    require_live_host_boundary("source-deployment")


def _create_pin_metadata(store, http, deployment_intent):
    """Create-only metadata primitive; not an authority entrypoint/model tool.

    A future reviewed source operation must verify all genuine originals/live
    authority before using this primitive. The public entrypoint is pending.
    """
    D.joint(http, store)
    fields = {
        "schema_version",
        "kind",
        "repository",
        "operation_id",
        "expected_main",
        "source_set_sha256",
        "composed_origin_proof_sha256",
        "native_core_proof_sha256",
        "whole_controller_admission_proof_sha256",
        "operator_compatibility_proof_sha256",
        "authority_snapshot_sha256",
        "independent_operation_review_sha256",
        "helper_sha256",
        "payload_sha256",
    }
    if (
        not isinstance(deployment_intent, dict)
        or set(deployment_intent) != fields
        or type(deployment_intent["schema_version"]) is not int
        or deployment_intent["schema_version"] != 1
        or deployment_intent["kind"] != "capture-source-deployment-intent-v1"
        or deployment_intent["repository"] != T.REPOSITORY
        or not T._hex(deployment_intent["operation_id"], 32)
        or not T._hex(deployment_intent["expected_main"], 40)
        or any(not T._hex(deployment_intent[k]) for k in fields if k.endswith("sha256"))
    ):
        raise ValueError("Invalid closed source deployment intent")
    head = deployment_intent["expected_main"]
    tag = "agent-visual-source-v1-" + head
    operation = deployment_intent["operation_id"]
    originals = []

    def read_pin():
        status, body, facts = http.request("git/ref/tags/" + tag)
        originals.append(
            {
                "http_status": status,
                "body_sha256": D.digest(body),
                "body": body.decode("utf-8", errors="backslashreplace"),
                "raw_body_base64": base64.b64encode(body).decode("ascii"),
                "response_facts": facts,
            }
        )
        if status == 200 and facts["body_complete"]:
            value = D.parse(body)
            if not T.source_identity(value, {"source_tag": tag, "expected_trusted_head": head}):
                raise D.Pending("Conflicting immutable source ref")
            return True
        if status == 404 and facts["body_complete"]:
            return False
        raise D.Pending("Exact source ref readback unavailable")

    def main_is_current():
        main = _get(http, "git/ref/heads/main", originals)
        if (
            main.get("ref") != "refs/heads/main"
            or main.get("object", {}).get("type") != "commit"
            or main["object"].get("sha") != head
        ):
            raise D.Pending("Main changed during source publication")

    try:
        with store.lease(
            ("source-deployments", "publication-" + D.digest(T.REPOSITORY.encode()) + ".lock")
        ):
            main_is_current()
            if read_pin():
                main_is_current()
                return {
                    "kind": "capture-source-pin-readback-v1",
                    "commit": head,
                    "source_tag": tag,
                    "created": False,
                }
            binding = {
                "purpose": "source-deployment",
                "operation_id": operation,
                "deployment_intent_sha256": D.digest(D.canonical(deployment_intent)),
                "source_tag": tag,
                "expected_trusted_head": head,
            }
            receipt = T.Journal(store, http).mutate(
                "source-ref-create",
                "git/refs",
                {"ref": "refs/tags/" + tag, "sha": head},
                binding,
                intent_id=operation,
                attempt_id=D.digest(D.canonical(binding))[:32],
            )
            if not read_pin():
                raise D.Pending("Source create unresolved; no second POST")
            main_is_current()
            return {
                "kind": "capture-source-pin-readback-v1",
                "commit": head,
                "source_tag": tag,
                "created": receipt["state"] == "created_response",
                "original_receipt_sha256": D.digest(D.canonical(receipt)),
            }
    finally:
        store.put(
            ("source-deployments", operation, D.digest(D.canonical(originals)) + ".json"),
            D.canonical(originals),
        )


def _read_source(root, relative, head, budget=None):
    """Read inert Git/file identity, never run a candidate or ambient hook."""
    args = ["git", "-c", "core.hooksPath=/dev/null", "ls-tree", "-z", head, "--", relative]
    record = subprocess.run(args, cwd=root, capture_output=True, check=True, timeout=5).stdout
    if budget is not None:
        budget.charge(len(record))
    prefix, separator, name = record.partition(b"\t")
    mode, kind, blob = prefix.split(b" ")
    if (
        separator != b"\t"
        or name != relative.encode() + b"\0"
        or mode != b"100644"
        or kind != b"blob"
    ):
        raise ValueError("Unsupported composed capture source member")
    # All members below trusted checkout; no symlink/late file replacement.
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parts = relative.split("/")
        for part in parts[:-1]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            before = os.fstat(file_fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > 2097152:
                raise ValueError("Unsafe composed capture source file")
            if budget is not None:
                budget.charge(before.st_size)
            raw = b""
            while len(raw) <= 2097152:
                block = os.read(file_fd, min(65536, 2097153 - len(raw)))
                if not block:
                    break
                raw += block
            after = os.fstat(file_fd)
            path_st = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
            if (
                D.identity(before) != D.identity(after)
                or D.identity(after) != D.identity(path_st)
                or len(raw) != before.st_size
            ):
                raise ValueError("Capture source changed while read")
        finally:
            os.close(file_fd)
    finally:
        os.close(fd)
    git_blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
    if git_blob != blob.decode():
        raise ValueError("Capture source differs from trusted commit")
    return raw, {
        "path": relative,
        "mode": "100644",
        "git_blob": git_blob,
        "sha256": D.digest(raw),
        "bytes": len(raw),
    }


def source_inventory(root, head, budget=None):
    budget = D.Budget() if budget is None else budget
    rows = []
    for path in SOURCE_PATHS:
        _raw, row = _read_source(root, path, head, budget)
        budget.charge(len(D.canonical(row)))
        rows.append(row)
    return rows


def _get(http, path, originals, expected_type=dict):
    status, body, facts = http.request(path)
    D.joint(http).charge(len(body), retained=True)
    originals.append(
        {
            "path": path,
            "http_status": status,
            "body_sha256": D.digest(body),
            "body": body.decode("utf-8", errors="backslashreplace"),
            "raw_body_base64": base64.b64encode(body).decode("ascii"),
            "response_facts": facts,
        }
    )
    D.joint(http).charge(len(D.canonical(originals[-1])), retained=True)
    if status != 200 or not facts["body_complete"]:
        raise D.Pending("Complete authenticated capture metadata unavailable")
    value = D.parse(body)
    if not isinstance(value, expected_type):
        raise ValueError("Invalid capture API object")
    return value


def guard_monitor(envelope, http, originals, event):
    """Resolve the complete eligible set; no replacement issue creation."""
    candidates = []
    for page in range(1, 6):
        rows = _get(
            http,
            "issues?state=open&labels=agent:state&per_page=100&page=" + str(page),
            originals,
            list,
        )
        if len(rows) > 100 or any(not isinstance(row, dict) for row in rows):
            raise ValueError("Invalid capture monitor page")
        for issue in rows:
            if (
                issue.get("title") == "GitHub agent integration monitor"
                and "pull_request" not in issue
            ):
                authenticate_comment(dict(event, issue=issue), envelope)
                candidates.append((issue["id"], issue["number"]))
        if len(rows) < 100:
            break
    else:
        raise D.Pending("Complete unique capture monitor unavailable")
    expected = (event["issue"]["id"], event["issue"]["number"])
    if candidates != [expected]:
        raise D.Pending("Capture monitor is missing, changed or ambiguous")


def guard_source(envelope, http, originals):
    source = envelope["source"]
    head = source["expected_trusted_head"]
    main = _get(http, "git/ref/heads/main", originals)
    pin = _get(http, "git/ref/tags/" + source["source_tag"], originals)
    if (
        main.get("ref") != "refs/heads/main"
        or pin.get("ref") != "refs/tags/" + source["source_tag"]
        or any(
            v.get("object", {}).get("type") != "commit" or v["object"].get("sha") != head
            for v in (main, pin)
        )
    ):
        raise ValueError("Trusted main/source tag changed")


def guard_candidate(envelope, http, originals):
    """candidate_base is the current feature head, not the reviewed dev base.

    The approved clarification16aa92 preserves separate private task/plan/Gate
    dev-base authority; this remote metadata check cannot issue that authority.
    """
    auth = envelope["authorization"]
    candidate, parent = auth["candidate"], auth["candidate_base"]
    commit = _get(http, "git/commits/" + candidate, originals)
    ref = _get(http, "git/ref/heads/agent-visual/" + candidate, originals)
    pr = _get(http, "pulls/" + str(auth["pr_number"]), originals)
    if (
        commit.get("sha") != candidate
        or not isinstance(commit.get("parents"), list)
        or len(commit["parents"]) != 1
        or commit["parents"][0].get("sha") != parent
        or ref.get("ref") != "refs/heads/agent-visual/" + candidate
        or ref.get("object", {}).get("type") != "commit"
        or ref["object"].get("sha") != candidate
        or type(pr.get("number")) is not int
        or pr["number"] != auth["pr_number"]
        or pr.get("state") != "open"
        or pr.get("head", {}).get("sha") != parent
        or pr.get("head", {}).get("repo", {}).get("full_name") != T.REPOSITORY
        or pr.get("base", {}).get("ref") != "dev"
        or pr.get("base", {}).get("repo", {}).get("full_name") != T.REPOSITORY
    ):
        raise ValueError("Temporary candidate parent/current PR identity changed")


def preflight(environment, http, *, now=None, originals=None):
    """Guard before either candidate checkout or the synthetic program."""
    now = int(time.time()) if now is None else now
    budget = D.joint(http)
    budget.charge(len(D.canonical(environment)))
    if (
        environment.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
        or environment.get("GITHUB_REPOSITORY") != T.REPOSITORY
        or environment.get("INPUT_PROTOCOL") != PROTOCOL
        or environment.get("GITHUB_RUN_ATTEMPT") != "1"
    ):
        raise ValueError("Unsupported capture receiver generation/event")
    purpose = environment.get("INPUT_PURPOSE")
    envelope = validate_envelope(
        environment.get("INPUT_ENVELOPE", "").encode(), expected_purpose=purpose, now=now
    )
    head, tag = (envelope["source"][k] for k in ("expected_trusted_head", "source_tag"))
    if (
        environment.get("GITHUB_SHA") != head
        or environment.get("INPUT_EXPECTED_TRUSTED_HEAD") != head
        or environment.get("GITHUB_REF") != "refs/tags/" + tag
    ):
        raise ValueError("Source-pinned capture receiver mismatch")
    comment_id = environment.get("INPUT_MONITOR_COMMENT_ID", "")
    run_id = environment.get("GITHUB_RUN_ID", "")
    if not re.fullmatch(r"[1-9][0-9]{0,15}", comment_id) or not re.fullmatch(
        r"[1-9][0-9]{0,15}", run_id
    ):
        raise ValueError("Invalid actual capture event identity")
    originals = [] if originals is None else originals
    guard_source(envelope, http, originals)
    comment = _get(http, "issues/comments/" + comment_id, originals)
    issue_url = comment.get("issue_url", "")
    if not re.fullmatch(
        r"https://api.github.com/repos/Karapsin/analytics_toolkit/issues/[1-9][0-9]*", issue_url
    ):
        raise ValueError("Foreign capture monitor issue")
    issue = _get(http, issue_url.split("/repos/" + T.REPOSITORY + "/", 1)[1], originals)
    event = {
        "repository": {"full_name": T.REPOSITORY},
        "sender": comment.get("user"),
        "issue": issue,
        "comment": comment,
    }
    if authenticate_comment(event, envelope) != int(comment_id):
        raise ValueError("Capture monitor identity mismatch")
    guard_monitor(envelope, http, originals, event)
    run = _get(http, "actions/runs/" + run_id, originals)
    if (
        run.get("id") != int(run_id)
        or run.get("run_attempt") != 1
        or type(run.get("run_attempt")) is not int
        or run.get("path") != T.WORKFLOW
        or run.get("event") != "workflow_dispatch"
        or run.get("head_sha") != head
        or run.get("head_branch") != tag
        or run.get("repository", {}).get("full_name") != T.REPOSITORY
        or not D.integer(run.get("workflow_id"))
    ):
        raise ValueError("Actual capture workflow/source/run mismatch")
    root = Path(__file__).resolve().parents[2]
    rows = source_inventory(root, head, budget)
    if D.digest(D.canonical(rows)) != envelope["source"]["source_set_sha256"]:
        raise ValueError("Composed capture source-set mismatch")
    if purpose == "receiver-rehearsal":
        auth = envelope["authorization"]
        manifest_raw, _ = _read_source(root, FIXTURE + "manifest.json", head, budget)
        program_raw, program_row = _read_source(root, FIXTURE + "sentinel.py", head, budget)
        manifest = D.parse(manifest_raw, 65536)
        if (
            D.digest(manifest_raw) != auth["fixture_manifest_sha256"]
            or D.digest(program_raw) != auth["fixture_program_sha256"]
            or manifest.get("id") != "capture-receiver-sentinel-v1"
            or manifest.get("program_sha256") != D.digest(program_raw)
            or manifest.get("program_git_blob") != program_row["git_blob"]
            or auth["case_id"] != "positive-fixed-source"
        ):
            raise ValueError("Fixed rehearsal fixture/recipe guard rejected")
    if purpose == "production":
        guard_candidate(envelope, http, originals)
    guard_source(envelope, http, originals)
    return {
        "schema_version": 1,
        "kind": "capture-profile-preflight-v1",
        "ok": True,
        "purpose": purpose,
        "envelope": envelope,
        "source_inventory": rows,
        "workflow_id": run["workflow_id"],
        "run_id": int(run_id),
        "run_attempt": 1,
        "metadata_originals": originals,
    }


def require_cross_run_bridge_history(envelope, http, originals):
    """Retain bounded navigation; it cannot establish genuine host intent history.

    The protected host issuer/current-row history boundary is not implemented
    in this first source unit. Fresh ephemeral files/nonce/comment or an empty
    eventually-consistent Actions list cannot stand in for its authority.
    """
    originals.append(
        {
            "kind": "capture-profile-history-capability-pending-v1",
            "envelope_sha256": D.digest(D.canonical(envelope)),
            "nonce": envelope["attempt"]["nonce"],
            "state": "pending",
        }
    )
    for page in range(1, 6):
        result = _get(
            http,
            "actions/workflows/agent-visual-profile.yml/runs?event=workflow_dispatch&per_page=100&page="
            + str(page),
            originals,
        )
        rows = result.get("workflow_runs")
        if not isinstance(rows, list) or len(rows) > 100:
            raise ValueError("Invalid prior capture navigation")
        if len(rows) < 100:
            break
    raise D.Pending("Genuine durable cross-run intent history boundary unavailable; no POST")


def dispatch_from_event(event, *, http, store, environment, now=None):
    """New App-only bridge; never reached by legacy scalar operations."""
    D.joint(http, store)
    body = event.get("comment", {}).get("body", "")
    if (
        not isinstance(body, str)
        or len(body.encode()) > 4096
        or not body.startswith("<!-- github-agent-dispatch\n")
        or not body.endswith("\n-->")
    ):
        raise ValueError("Invalid profile dispatch body")
    task = D.parse(body[len("<!-- github-agent-dispatch\n") : -4].encode(), 4096)
    if (
        not isinstance(task, dict)
        or set(task) != {"operation", "value"}
        or not isinstance(task["value"], dict)
    ):
        raise ValueError("Invalid profile dispatch fields")
    envelope = validate_envelope(
        D.canonical(task["value"]),
        expected_purpose=task["value"].get("purpose"),
        now=int(time.time()) if now is None else now,
    )
    if task["operation"] != envelope["operation"] or body != monitor_body(envelope):
        raise ValueError("Mixed profile dispatch operation/body")
    comment_id = authenticate_comment(event, envelope)
    head = envelope["source"]["expected_trusted_head"]
    if (
        environment.get("GITHUB_REPOSITORY") != T.REPOSITORY
        or environment.get("GITHUB_SHA") != head
        or environment.get("GITHUB_RUN_ATTEMPT") != "1"
        or not re.fullmatch(r"[1-9][0-9]{0,15}", environment.get("GITHUB_RUN_ID", ""))
    ):
        raise ValueError("Trusted dispatch source/run mismatch")
    originals = []
    guard_source(envelope, http, originals)
    fetched = _get(http, "issues/comments/" + str(comment_id), originals)
    issue = _get(http, "issues/" + str(event["issue"]["number"]), originals)
    authenticated = dict(event, comment=fetched, issue=issue)
    authenticate_comment(authenticated, envelope)
    guard_monitor(envelope, http, originals, authenticated)
    workflow = _get(http, "actions/workflows/agent-visual-profile.yml", originals)
    if (
        workflow.get("path") != T.WORKFLOW
        or workflow.get("state") != "active"
        or not D.integer(workflow.get("id"))
    ):
        raise D.Pending("Unique capture receiver is unavailable")
    try:
        require_cross_run_bridge_history(envelope, http, originals)
    finally:
        store.put(("bridge-navigation.json",), D.canonical(originals))
    nonce = envelope["attempt"]["nonce"]
    bridge_run = int(environment["GITHUB_RUN_ID"])
    attempt_id = D.digest(D.canonical({"comment_id": comment_id, "bridge_run": bridge_run}))[:32]
    binding = {
        "purpose": envelope["purpose"],
        "operation": envelope["operation"],
        "nonce": nonce,
        "envelope_sha256": D.digest(D.canonical(envelope)),
        "monitor_comment_id": comment_id,
        "workflow_id": workflow["id"],
        "workflow_path": T.WORKFLOW,
        "source_tag": envelope["source"]["source_tag"],
        "expected_trusted_head": head,
        "bridge_run_id": bridge_run,
        "bridge_run_attempt": 1,
        "return_run_details": False,
    }
    store.put(("capture-transport", nonce, attempt_id, "metadata.json"), D.canonical(originals))
    payload = {
        "ref": binding["source_tag"],
        "return_run_details": False,
        "inputs": {
            "protocol": PROTOCOL,
            "purpose": envelope["purpose"],
            "expected_trusted_head": head,
            "monitor_comment_id": str(comment_id),
            "envelope": D.canonical(envelope).decode(),
        },
    }
    return T.Journal(store, http).mutate(
        "workflow-dispatch",
        "actions/workflows/agent-visual-profile.yml/dispatches",
        payload,
        binding,
        intent_id=nonce,
        attempt_id=attempt_id,
    )


def retain_capacity_pending(root, budget):
    with D.Store(root, budget=budget) as recovery:
        recovery.put(
            ("capacity-pending.json",),
            D.canonical(
                {
                    "schema_version": 1,
                    "state": "pending",
                    "reason": "joint-budget",
                    "used_bytes": budget.used,
                    "limit_bytes": budget.limit,
                }
            ),
            emergency=True,
        )


def runner_main():
    # Every write is under a fresh trusted runner.temp root; no model path.
    base = Path(os.environ["RUNNER_TEMP"]).resolve()
    root = base / ("capture-profile-" + uuid.uuid4().hex)
    root.mkdir(mode=0o700)
    originals = []
    http = T.HTTP()
    budget = D.joint(http)
    inputs = {
        k: os.environ.get(k)
        for k in (
            "INPUT_PROTOCOL",
            "INPUT_PURPOSE",
            "INPUT_EXPECTED_TRUSTED_HEAD",
            "INPUT_MONITOR_COMMENT_ID",
            "INPUT_ENVELOPE",
            "GITHUB_SHA",
            "GITHUB_REF",
            "GITHUB_RUN_ID",
            "GITHUB_RUN_ATTEMPT",
        )
    }
    with D.Store(root, budget=budget) as store:
        store.put(("input.json",), D.canonical(inputs))
    try:
        report = preflight(dict(os.environ), http, originals=originals)
    except (Exception, KeyboardInterrupt) as exc:
        if isinstance(exc, D.BudgetPending):
            retain_capacity_pending(root, budget)
            with Path(os.environ["GITHUB_OUTPUT"]).open("a") as stream:
                stream.write("artifact_root=" + str(root) + "\n")
            raise SystemExit("Profile receiver joint capacity pending; no execution") from None
        with D.Store(root, budget=budget) as recovery:
            for number, observation in enumerate(http.pending_observations):
                recovery.put(
                    ("private-delivery-" + str(number) + ".json",),
                    D.canonical(observation),
                    emergency=True,
                )
        report = {
            "schema_version": 1,
            "kind": "capture-profile-preflight-v1",
            "ok": False,
            "reason": "receiver-preflight-reject",
            "sentinel_executed": False,
            "visual_approval": False,
            "prerequisite_satisfied": False,
        }
        try:
            with D.Store(root, budget=budget) as store:
                store.put(("preflight.json",), D.canonical(report))
                store.put(("metadata-originals.json",), D.canonical(originals))
                index = D.canonical(
                    {"input": inputs, "metadata_originals": originals, "preflight": report}
                )
                store.put(("raw-index.json",), index)
                try:
                    envelope = validate_envelope(
                        inputs["INPUT_ENVELOPE"].encode(),
                        expected_purpose="receiver-rehearsal",
                        now=int(time.time()),
                    )
                    actual_run = inputs["GITHUB_RUN_ID"]
                    if not isinstance(actual_run, str) or not re.fullmatch(
                        "[1-9][0-9]{0,15}", actual_run
                    ):
                        raise ValueError("No actual receiver run identity")
                    observation = {
                        "schema_version": 1,
                        "kind": "receiver-rehearsal-rejection-observation-v1",
                        "purpose": "receiver-rehearsal",
                        "operation_id": envelope["authorization"]["operation_id"],
                        "case_id": envelope["authorization"]["case_id"],
                        "nonce": envelope["attempt"]["nonce"],
                        "envelope_sha256": D.digest(D.canonical(envelope)),
                        "stage": "receiver-preflight-reject",
                        "workflow_run_id": int(actual_run),
                        "sentinel_executed": False,
                        "visual_approval": False,
                        "prerequisite_satisfied": False,
                        "raw_index_sha256": D.digest(index),
                    }
                    validate_type(observation, "rehearsalRejectionObservation")
                    store.put(("rehearsal-rejection.json",), D.canonical(observation))
                except (ValueError, UnicodeError, AttributeError):
                    pass  # Truly unparsed input remains diagnostics, never a fabricated purpose/result.
        except D.BudgetPending:
            retain_capacity_pending(root, budget)
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as stream:
            stream.write("artifact_root=" + str(root) + "\n")
        raise SystemExit(
            "Profile receiver preflight rejected; candidate and sentinel skipped"
        ) from None
    with D.Store(root, budget=budget) as store:
        store.put(("preflight.json",), D.canonical(report))
        store.put(
            ("budget.json",), D.canonical({"used": budget.used + 1024, "limit": budget.limit})
        )
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as stream:
        stream.write(
            "ok=true\npurpose=" + report["purpose"] + "\nartifact_root=" + str(root) + "\n"
        )
        if report["purpose"] == "production":
            stream.write("candidate=" + report["envelope"]["authorization"]["candidate"] + "\n")


def run_fixed_rehearsal(preflight_root):
    """Runner-only fixed source fixture; no PR checkout, host fixture execution."""
    root = Path(preflight_root)
    if root.parent != Path(os.environ["RUNNER_TEMP"]).resolve() or not re.fullmatch(
        "capture-profile-[0-9a-f]{32}", root.name
    ):
        raise ValueError("Unsafe receiver preflight root")
    with D.Store(root) as initial:
        try:
            prior = D.parse(initial.read(("budget.json",), 1024))
        except FileNotFoundError:
            # Only synthetic component fixtures omit the runtime checkpoint.
            raise D.Pending("Joint preflight budget checkpoint missing") from None
    budget = D.Budget(prior["limit"], prior["used"])
    with D.Store(root, budget=budget) as store:
        report = D.parse(store.read(("preflight.json",)))
    if (
        report.get("kind") != "capture-profile-preflight-v1"
        or report.get("ok") is not True
        or report.get("purpose") != "receiver-rehearsal"
    ):
        raise ValueError("No genuine rehearsal preflight")
    envelope = validate_envelope(
        D.canonical(report["envelope"]), expected_purpose="receiver-rehearsal", now=int(time.time())
    )
    auth = envelope["authorization"]
    if auth["case_id"] != "positive-fixed-source":
        raise ValueError("No negative recipe executes the sentinel")
    trusted = Path(__file__).resolve().parents[2]
    if (
        source_inventory(trusted, envelope["source"]["expected_trusted_head"], budget)
        != report["source_inventory"]
    ):
        raise ValueError("Preflight source changed")
    program, _ = _read_source(
        trusted, FIXTURE + "sentinel.py", envelope["source"]["expected_trusted_head"], budget
    )
    manifest, _ = _read_source(
        trusted, FIXTURE + "manifest.json", envelope["source"]["expected_trusted_head"], budget
    )
    if (
        D.digest(program) != auth["fixture_program_sha256"]
        or D.digest(manifest) != auth["fixture_manifest_sha256"]
    ):
        raise ValueError("Preflight fixture changed")
    # All candidate/stream writers live OUTSIDE the upload root from creation.
    # Even failed helper quarantine cannot put a live writer under root.
    execution = root.parent / ("capture-private-execution-" + uuid.uuid4().hex)
    execution.mkdir(mode=0o700)
    work = execution / "fixture"
    logs = execution / "streams"
    work.mkdir(mode=0o700)
    logs.mkdir(mode=0o700)
    with D.Store(work, budget=budget) as stage:
        stage.put(("sentinel.py",), program)
    # Fixed source import only after verified source guard. Neither GH_TOKEN nor
    # arbitrary environment enters Sandbox's exact allowlisted environment.
    sandbox = D.sibling("capture_sandbox").Sandbox(
        work, trusted, execution / "resources.json", python_version="3.11"
    )
    isolation = D.sibling("capture_isolation")
    settings = {
        "nonce": envelope["attempt"]["nonce"],
        "trusted_head": envelope["source"]["expected_trusted_head"],
        "purpose": "receiver-rehearsal",
    }
    identity = {
        "candidate": settings["trusted_head"],
        "repository": T.REPOSITORY,
        "run_id": report["run_id"],
        "run_attempt": 1,
    }
    stream_limit = budget.response_limit(2 * 1024 * 1024)
    budget.response(stream_limit, stream_limit)
    stream_report = None
    cleanup_complete = False
    error = None
    try:
        sandbox.setup()
        if sandbox.image != NATIVE_IMAGE_311:
            raise D.Pending("Resolved image is outside the fixed native3.11 profile")
        sandbox.verify()
        container = sandbox.create(
            "capture",
            ["python", "-I", "/work/sentinel.py", D.canonical(settings).decode()],
            identity,
        )
        sandbox.validate_container(container)
        stream_report = isolation.contained_output(
            container,
            "sentinel",
            logs,
            30,
            limit=stream_limit,
            quarantine_root=root.parent,
        )
        sandbox.remove(container)
    except (Exception, KeyboardInterrupt) as exc:
        error = type(exc).__name__
    finally:
        try:
            sandbox.cleanup()
            cleanup_complete = True
        except (Exception, KeyboardInterrupt) as exc:
            error = type(exc).__name__
    expected = dict(
        settings,
        marker="receiver-rehearsal-v1",
        visual_approval=False,
        prerequisite_satisfied=False,
    )
    marker = None
    if cleanup_complete:
        try:
            with D.Store(work, budget=budget) as stage:
                marker = D.parse(stage.read(("sentinel-result.json",), 65536))
        except (OSError, ValueError, D.Pending):
            error = "IncompleteSentinelOutput"
    good = (
        cleanup_complete
        and marker == expected
        and stream_report is not None
        and stream_report.get("ok") is True
        and stream_report.get("outputs_complete") is True
        and stream_report.get("readers_stopped") is True
        and stream_report.get("logs_safe") is True
        and not stream_report.get("quarantined_logs")
        and error is None
    )
    raw_index = {
        "input": report,
        "streams": stream_report,
        "marker": marker,
        "cleanup_complete": cleanup_complete,
        "error_type": error,
        "immutable_image": sandbox.image,
    }
    with D.Store(root, budget=budget) as store:
        try:
            with D.Store(execution, budget=budget) as private:
                journal = private.read(("resources.json",), 2097152)
            raw_index["resource_journal"] = {
                "sha256": D.digest(journal),
                "bytes": len(journal),
                "raw_base64": base64.b64encode(journal).decode("ascii"),
            }
        except (OSError, D.Pending):
            good = False
            raw_index["resource_journal"] = None
        if (
            stream_report is not None
            and stream_report.get("logs_safe") is True
            and stream_report.get("readers_stopped") is True
            and not stream_report.get("quarantined_logs")
        ):
            # Copy only descriptor-verified finalized parts, never live paths.
            with D.Store(logs, budget=budget) as private:
                for stream in stream_report.get("streams", {}).values():
                    for part in stream.get("parts", []):
                        body = private.read((part["path"],), 2 * 1024 * 1024)
                        if len(body) != part["bytes"] or D.digest(body) != part["sha256"]:
                            raise D.Pending("Finalized sentinel stream identity changed")
                        store.put(("streams", part["path"]), body)
        if not good:
            store.put(("rehearsal-failure.json",), D.canonical(raw_index))
            raise D.Pending("Fixed sentinel failed; retain complete/incomplete diagnostics")
        raw = D.canonical(raw_index)
        store.put(("raw-index.json",), raw)
        result = {
            "schema_version": 1,
            "kind": "receiver-rehearsal-result-v1",
            "purpose": "receiver-rehearsal",
            "repository": T.REPOSITORY,
            "operation_id": auth["operation_id"],
            "case_id": auth["case_id"],
            "nonce": envelope["attempt"]["nonce"],
            "envelope_sha256": D.digest(D.canonical(envelope)),
            "admission_receipt_sha256": auth["admission_receipt_sha256"],
            "source_set_sha256": envelope["source"]["source_set_sha256"],
            "trusted_head": settings["trusted_head"],
            "fixture_manifest_sha256": auth["fixture_manifest_sha256"],
            "workflow_id": report["workflow_id"],
            "run_id": report["run_id"],
            "run_attempt": 1,
            "guard_result": "pass",
            "sentinel_executed": True,
            "visual_approval": False,
            "prerequisite_satisfied": False,
            "raw_index_sha256": D.digest(raw),
        }
        validate_type(result, "rehearsalResult")
        store.put(("rehearsal-result.json",), D.canonical(result))
    return result


def bridge_main(event):
    """Fixed trusted runner output root; private POST journal survives upload."""
    env = dict(os.environ)
    root = Path(env["RUNNER_TEMP"]).resolve() / ("capture-profile-bridge-" + uuid.uuid4().hex)
    root.mkdir(mode=0o700)
    http = T.HTTP()
    budget = D.joint(http)
    try:
        with D.Store(root, budget=budget) as store:
            result = dispatch_from_event(event, http=http, store=store, environment=env)
            with Path(env["GITHUB_OUTPUT"]).open("a") as stream:
                stream.write(
                    "artifact_name=capture-profile-dispatch-"
                    + str(result["monitor_comment_id"])
                    + "-"
                    + result["nonce"]
                    + "-"
                    + str(result["bridge_run_id"])
                    + "-1\n"
                )
            return result
    except (Exception, KeyboardInterrupt):
        # Parsed identity is not required for a truthful negative. Authenticate
        # only actual App/event fields; do not echo arbitrary exception text.
        comment = event.get("comment", {})
        app = comment.get("performed_via_github_app", {})
        user = comment.get("user", {})
        if (
            D.integer(comment.get("id"))
            and user.get("id") == 340139075
            and user.get("type") == "Bot"
            and app.get("id") == 5250620
            and app.get("slug") == "analytics-toolkit-agent-717959"
            and re.fullmatch("[1-9][0-9]{0,15}", env.get("GITHUB_RUN_ID", ""))
        ):
            raw = str(comment.get("body", "")).encode("utf-8")
            pending_identity = None
            try:
                parsed = D.parse(raw[len("<!-- github-agent-dispatch\n") : -4], 4096)
                value = parsed["value"]
                validate_envelope(
                    D.canonical(value), expected_purpose=value.get("purpose"), now=int(time.time())
                )
                pending_identity = value
            except (ValueError, KeyError, TypeError, UnicodeError):
                pass
            with D.Store(root, budget=budget) as store:
                store.put(
                    (
                        "unparsed-rejection.json"
                        if pending_identity is None
                        else "bridge-pending.json",
                    ),
                    D.canonical(
                        {
                            "schema_version": 1,
                            "kind": "capture-profile-unparsed-rejection-v1"
                            if pending_identity is None
                            else "capture-profile-bridge-pending-v1",
                            "comment_id": comment["id"],
                            "bridge_run_id": int(env["GITHUB_RUN_ID"]),
                            "bridge_run_attempt": int(env["GITHUB_RUN_ATTEMPT"]),
                            "raw_input_sha256": D.digest(raw),
                            "reason": "profile-dispatch-refused",
                            "purpose": None
                            if pending_identity is None
                            else pending_identity["purpose"],
                            "nonce": None
                            if pending_identity is None
                            else pending_identity["attempt"]["nonce"],
                            "state": "rejected" if pending_identity is None else "pending",
                        }
                    ),
                )
                if len(raw) <= 4096:
                    store.put(("raw-input.bin",), raw)
            with Path(env["GITHUB_OUTPUT"]).open("a") as stream:
                stream.write(
                    "artifact_name=capture-profile-unparsed-rejection-"
                    + str(comment["id"])
                    + "-"
                    + env["GITHUB_RUN_ID"]
                    + "\n"
                )
        raise
    finally:
        with D.Store(root, budget=budget) as store:
            for index, observation in enumerate(http.pending_observations):
                store.put(("private-delivery-" + str(index) + ".json",), D.canonical(observation))
        with Path(env["GITHUB_OUTPUT"]).open("a") as stream:
            stream.write("artifact_root=" + str(root) + "\n")


if __name__ == "__main__":
    if sys.argv[1:] == ["preflight"]:
        runner_main()
    elif sys.argv[1:] == ["rehearsal"]:
        run_fixed_rehearsal(os.environ["PREFLIGHT_ROOT"])
    else:
        raise SystemExit("Unsupported trusted receiver entrypoint")
