"""Host-owned pauses, owner decisions and a durable notification outbox."""
from __future__ import annotations

import json
import base64
import hashlib
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

PAUSED = "agent:needs-user"
PHASES = frozenset({"agent:writing", "agent:planning", "agent:reviewing",
                    "agent:queued", "agent:testing", "agent:ready"})
_STATUS_LOCKS: dict[tuple[str, str, int], Any] = {}
_STATUS_GUARD = threading.Lock()
_STATUS_DEPTH = threading.local()
_COORDINATOR_AUTHORITY: Any = None
_COORDINATOR_GUARD = threading.Lock()


def _coordinator_authority() -> Any:
    """Load only the fixed protected siblings, isolated per controller instance."""
    with _COORDINATOR_GUARD:
        return _load_coordinator_authority()


def _load_coordinator_authority() -> Any:
    global _COORDINATOR_AUTHORITY
    if _COORDINATOR_AUTHORITY is None:
        import importlib.util
        name = "_atk_coordinator_" + secrets.token_hex(16)
        path = Path(__file__).with_name("coordinator_authority.py")
        spec = importlib.util.spec_from_file_location(name, path, submodule_search_locations=[str(path.parent)])
        if spec is None or spec.loader is None:
            raise RuntimeError("Checked coordinator implementation unavailable")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            for key in list(sys.modules):
                if key == name or key.startswith(name + "."):
                    sys.modules.pop(key, None)
            raise
        _COORDINATOR_AUTHORITY = module
    return _COORDINATOR_AUTHORITY


def __getattr__(name: str) -> Any:
    if name == "CoordinatorAuthorityPending":
        return _coordinator_authority().CoordinatorAuthorityPending
    raise AttributeError(name)


def prerequisite_authority_snapshot(repository: str, scope: dict[str, Any]) -> dict[str, Any]:
    """Observe the full receipt-selected control scope without authority writes."""
    return _coordinator_authority().prerequisite_authority_snapshot(repository, scope)


def renew_coordinator_checked(
    gh: Any, pr: dict[str, Any], binding: dict[str, Any], task_digest: str, *,
    scope: dict[str, Any], expected_authority: dict[str, Any], proof_sha256: str,
    caller_binding: dict[str, Any],
) -> dict[str, Any]:
    """CAS only the matching hold to renewed planning; no external permission."""
    return _coordinator_authority().renew_coordinator_checked(
        gh, pr, binding, task_digest, scope=scope, expected_authority=expected_authority,
        proof_sha256=proof_sha256, caller_binding=caller_binding)


@contextmanager
def _status_lock(repository: str, number: int) -> Iterator[None]:
    """Serialize this host's display writers per PR, with bounded acquisition."""
    directory = os.environ.get("AGENT_STATE_DIR", "")
    key = (str(Path(directory).resolve()) if directory else "", repository, number)
    with _STATUS_GUARD:
        lock = _STATUS_LOCKS.setdefault(key, threading.RLock())
    if not lock.acquire(timeout=2):
        raise TimeoutError("PR status display is busy")
    descriptor = None
    depths = getattr(_STATUS_DEPTH, "keys", {})
    _STATUS_DEPTH.keys = depths
    nested = depths.get(key, 0)
    try:
        if not nested and directory and os.name == "posix":
            import fcntl
            root = Path(directory) / "status-locks"
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            name = hashlib.sha256(f"{repository}:{number}".encode()).hexdigest() + ".lock"
            descriptor = os.open(root / name, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        depths[key] = nested + 1
        yield
    finally:
        if nested:
            depths[key] = nested
        else:
            depths.pop(key, None)
        if descriptor is not None:
            os.close(descriptor)
        lock.release()


def status_transition(
    gh: Any, number: int, head: str,
    choose: Callable[[dict[str, Any]], str | None], *,
    attention: Callable[[dict[str, Any]], bool] | None = None,
    remove: frozenset[str] = frozenset(), expected_state: str = "open",
) -> dict[str, Any] | None:
    """Converge owned display labels without changing any private authority.

    None from choose preserves unknown phase ownership. Existing attention is
    preserved unless an explicit read-only classification is supplied. Each
    successful mutation is followed by fresh metadata; failures are retried by
    normal reconciliation. GitHub has no cross-request label compare-and-swap.
    """
    if expected_state not in {"open", "closed"} or not remove <= PHASES:
        raise ValueError("Invalid status transition")
    deadline = time.monotonic() + 30

    def budget() -> None:
        if time.monotonic() >= deadline:
            raise TimeoutError("PR status display did not converge")

    def label_names(value: Any, maximum: int) -> set[str]:
        if type(value) is not list or len(value) > maximum:
            raise ValueError("Invalid PR status labels")
        names: set[str] = set()
        for item in value:
            name = item.get("name") if type(item) is dict else None
            if type(name) is not str or not name or len(name) > 100 or name in names:
                raise ValueError("Invalid or repeated PR status label")
            names.add(name)
        return names

    def labels() -> set[str]:
        names: set[str] = set()
        for page in range(1, 12):
            budget()
            batch = gh.api(f"issues/{number}/labels?per_page=100&page={page}")
            current = label_names(batch, 100)
            if names & current or len(names) + len(current) > 1000:
                raise ValueError("PR status label pagination repeated or exceeded capacity")
            names.update(current)
            if len(batch) < 100:
                return names
        raise ValueError("PR status label pagination incomplete")

    def snapshot() -> tuple[dict[str, Any], set[str], bool, str | None] | None:
        budget()
        fresh = gh.pull(number)
        if fresh["state"] != expected_state or fresh["head"]["sha"] != head:
            return None
        names = labels()
        fresh = dict(fresh, labels=[{"name": name} for name in sorted(names)])
        if expected_state == "closed":
            required = PAUSED in names  # Closed cleanup changes phases only.
        else:
            required = (bool(attention(fresh)) if attention is not None else PAUSED in names)
            required = owner_attention(gh, fresh) or required
        target = "" if required else choose(fresh)
        if target is not None and target != "" and target not in PHASES:
            raise ValueError("Unknown PR status phase")
        budget()
        latest = gh.pull(number)
        if latest["state"] != expected_state or latest["head"]["sha"] != head:
            return None
        if expected_state == "open" and (owner_attention(gh, latest) or (
                attention is None and any(item["name"] == PAUSED for item in latest.get("labels", [])))):
            required, target = True, ""
        return dict(latest, labels=fresh["labels"]), names, required, target

    def desired(state: tuple[dict[str, Any], set[str], bool, str | None]) -> set[str]:
        _fresh, names, required, target = state
        result = names - (PHASES & remove)
        if target is not None:
            result -= PHASES
            if target:
                result.add(target)
        if required:
            result.add(PAUSED)
        elif attention is not None and expected_state == "open":
            result.discard(PAUSED)
        return result

    with _status_lock(gh.repository, number):
        for _ in range(24):
            state = snapshot()
            if state is None:
                return None  # Never transfer the old head's attention decision.
            if desired(state) == state[1]:
                return state[0]
            # Reproject current activity/private attention and complete labels
            # immediately before replacement. The callback owns its data source;
            # this display helper does not invent a fresh request/authority API.
            state = snapshot()
            if state is None:
                return None
            wanted = desired(state)
            if wanted == state[1]:
                return state[0]
            if len(wanted) > 1000:
                raise ValueError("Projected PR status labels exceeded capacity")
            budget()
            try:
                response = gh.api(f"issues/{number}/labels", "PUT", {"labels": sorted(wanted)})
                label_names(response, 1000)
            except Exception:
                # A lost response can follow an accepted replacement. Only fresh
                # authenticated reads may establish convergence; otherwise keep
                # the diagnostic and let ordinary reconciliation retry later.
                observed = snapshot()
                if observed is None:
                    return None
                if desired(observed) == observed[1]:
                    return observed[0]
                raise
        raise TimeoutError("PR status display did not converge")


def _preserve_phase(pr: dict[str, Any]) -> str:
    phases = {item["name"] for item in pr.get("labels", [])} & PHASES
    return next(iter(phases)) if len(phases) == 1 else ""


def _attention_display(gh: Any, pr: dict[str, Any]) -> None:
    fresh = status_transition(gh, pr["number"], pr["head"]["sha"], _preserve_phase,
                              attention=lambda latest: False)
    if fresh is not None:
        pr["labels"] = fresh["labels"]


def connect() -> sqlite3.Connection:
    directory = Path(os.environ["AGENT_STATE_DIR"])
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / "review_control.sqlite3"
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS pauses (
          repository TEXT, number INTEGER, head TEXT, reason TEXT, created REAL,
          PRIMARY KEY(repository, number));
        CREATE TABLE IF NOT EXISTS coordinator_holds (
          repository TEXT, number INTEGER, request TEXT, binding TEXT,
          PRIMARY KEY(repository, number, request));
        CREATE TABLE IF NOT EXISTS coordinator_renewals (
          repository TEXT, number INTEGER, request TEXT, binding TEXT, head TEXT, digest TEXT,
          PRIMARY KEY(repository, number, request));
        CREATE TABLE IF NOT EXISTS coordinator_checked_renewals (
          repository TEXT NOT NULL, number INTEGER NOT NULL, request TEXT NOT NULL,
          binding_sha256 TEXT NOT NULL, transition_sha256 TEXT NOT NULL UNIQUE,
          receipt_json TEXT NOT NULL, receipt_sha256 TEXT NOT NULL, created REAL NOT NULL,
          PRIMARY KEY(repository,number,request,binding_sha256));
        CREATE TABLE IF NOT EXISTS feature_amendments (
          repository TEXT, number INTEGER, head TEXT, created REAL,
          PRIMARY KEY(repository, number));
        CREATE TABLE IF NOT EXISTS attention_hints (
          repository TEXT, number INTEGER, head TEXT, pause TEXT, required INTEGER, context TEXT,
          PRIMARY KEY(repository, number));
        CREATE TABLE IF NOT EXISTS notifications (
          key TEXT PRIMARY KEY, kind TEXT, payload TEXT, sent INTEGER DEFAULT 0,
          retry REAL DEFAULT 0, token TEXT, message_id INTEGER);
        CREATE TABLE IF NOT EXISTS approvals (
          repository TEXT, number INTEGER, head TEXT, actor INTEGER, callback TEXT,
          PRIMARY KEY(repository, number, head));
        CREATE TABLE IF NOT EXISTS vetoes (
          repository TEXT, number INTEGER, head TEXT, created REAL,
          PRIMARY KEY(repository, number, head));
        CREATE TABLE IF NOT EXISTS feature_contexts (repository TEXT, branch TEXT, digest TEXT, PRIMARY KEY(repository,branch,digest));
        CREATE TABLE IF NOT EXISTS features (repository TEXT, branch TEXT, digest TEXT, PRIMARY KEY(repository,branch));
        CREATE TABLE IF NOT EXISTS cursors (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS corrections (
          repository TEXT, number INTEGER, head TEXT, digest TEXT, reason TEXT,
          changes TEXT, created REAL, PRIMARY KEY(repository,number));
        CREATE TABLE IF NOT EXISTS assessments (
          repository TEXT, number INTEGER, head TEXT, digest TEXT, changes TEXT,
          PRIMARY KEY(repository,number,head));
    """)
    path.chmod(0o600)
    return db


def enqueue(key: str, kind: str, payload: dict[str, Any], *, buttons: bool = False) -> None:
    with connect() as db:
        db.execute("INSERT OR IGNORE INTO notifications(key,kind,payload,token) VALUES (?,?,?,?)",
                   (key, kind, json.dumps(payload), secrets.token_urlsafe(18) if buttons else None))


def pending() -> list[dict[str, Any]]:
    with connect() as db:
        events = []
        rows = db.execute(
            "SELECT * FROM notifications WHERE sent=0 AND retry<=? ORDER BY rowid LIMIT 200",
            (time.time(),)).fetchall()
        for row in rows:
            payload = json.loads(row["payload"])
            if (row["kind"] == "request" and payload.get("telegram_feature") is False
                    and not row["key"].endswith((":waiting", ":context-blocked"))):
                # Retain the original payload and receipts, but do not deliver
                # local-worker status retries (including already queued rows).
                db.execute("UPDATE notifications SET sent=-1 WHERE key=? AND sent=0", (row["key"],))
                continue
            events.append(dict(row))
            if len(events) == 20:
                break
        return events


def delivery(key: str, *, message_id: int | None = None, delay: float = 0) -> None:
    with connect() as db:
        if message_id is not None:
            db.execute("UPDATE notifications SET sent=1,message_id=? WHERE key=?", (message_id, key))
        else:
            db.execute("UPDATE notifications SET retry=? WHERE key=?", (time.time() + delay, key))


def _notification_metadata(
    pr: dict[str, Any], category: str, task_digest: str | None = None,
) -> dict[str, Any]:
    """Lookup hints only: the companion must verify authenticated request bindings."""
    return {
        "notification_category": category,
        "request_origin": {
            "branch": pr["head"].get("ref"),
            "task_digest": task_digest,
        },
    }


def pause(gh: Any, pr: dict[str, Any], reason: str, changes: str) -> None:
    head, number = pr["head"]["sha"], pr["number"]
    with connect() as db:
        db.execute("INSERT OR IGNORE INTO pauses VALUES (?,?,?,?,?)",
                   (gh.repository, number, head, reason, time.time()))
    _attention_display(gh, pr)
    marker = f"<!-- agent-rejection-{head} -->"
    comments = gh.pages(f"issues/{number}/comments?per_page=100")
    owner = gh.repository.split("/")[0]
    context_blocked = reason == "Feature context does not match the host-owned request."
    coordinator_guidance = (
        "Coordinator must restore and verify the matching authenticated task context. "
        "The provenance hold remains active; no new owner answer or approval is inferred."
    )
    if not any(marker in item["body"] for item in comments):
        introduction = ("Automatic processing is blocked by a task-context mismatch."
                        if context_blocked else f"@{owner} This PR requires user input.")
        guidance = (coordinator_guidance if context_blocked else
                    "Automatic corrections are paused. To resume, post exactly:\n"
                    + f"`/agent resume {head}`")
        gh.comment(number, marker + "\n" + introduction + "\n\n"
                   + "What changed: " + changes + "\n\nWhy rejected:\n" + reason
                   + "\n\n" + guidance)
    if context_blocked:
        recovery_command = coordinator_guidance
    elif pr["head"].get("ref", "").startswith("codex/telegram-feature-"):
        request_id = pr["head"]["ref"][len("codex/telegram-feature-"):].split("-", 1)[0]
        recovery_command = "/reply_agent " + request_id + " <correction>"
    else:
        recovery_command = "/agent resume " + head
    enqueue(f"reject:{gh.repository}:{number}:{head}", "rejection",
            {"repository": gh.repository, "number": number, "head": head,
             "url": pr.get("html_url", f"https://github.com/{gh.repository}/pull/{number}"),
             "title": pr.get("title", "PR " + str(number)), "changes": changes,
             "reason": reason, "command": recovery_command,
             "coordinator_blocked": context_blocked,
             **_notification_metadata(pr, "coordinator_blocked" if context_blocked else "paused")})


def hold_coordinator(gh: Any, pr: dict[str, Any], binding: dict[str, Any]) -> None:
    """Host-only dependency gate, separate from owner decisions and questions."""
    if (binding["repository"] != gh.repository or not binding["request_id"]
            or type(binding["revision"]) is not int or binding["revision"] < 0):
        raise ValueError("Coordinator hold identity differs from the request.")
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT binding FROM coordinator_holds WHERE repository=? AND number=? AND request=?",
            (gh.repository, pr["number"], binding["request_id"]),
        ).fetchone()
        if row is not None and json.loads(row["binding"])["revision"] > binding["revision"]:
            return  # A stale reconciliation cannot replace a newer request.
        renewed = db.execute(
            "SELECT binding FROM coordinator_renewals WHERE repository=? AND number=? AND request=?",
            (gh.repository, pr["number"], binding["request_id"]),
        ).fetchone()
        if renewed is not None and (
                renewed["binding"] == json.dumps(binding, sort_keys=True)
                or json.loads(renewed["binding"])["revision"] > binding["revision"]):
            return  # Intake may replay after renewal committed but before it advanced.
        db.execute("INSERT OR REPLACE INTO coordinator_holds VALUES (?,?,?,?)",
                   (gh.repository, pr["number"], binding["request_id"],
                    json.dumps(binding, sort_keys=True)))


def coordinator_held(gh: Any, pr: dict[str, Any]) -> bool:
    with connect() as db:
        return db.execute("SELECT 1 FROM coordinator_holds WHERE repository=? AND number=?",
                          (gh.repository, pr["number"])).fetchone() is not None


def _coordinator_target_valid(gh: Any, pr: dict[str, Any], binding: Any) -> bool:
    if not isinstance(binding, dict):
        return False
    head, digest = binding.get("candidate_head"), binding.get("task_digest")
    if not all((isinstance(head, str), isinstance(digest, str))):
        return False
    return all((
        pr.get("state") == "open", binding.get("repository") == gh.repository,
        isinstance(binding.get("request_id"), str), bool(binding.get("request_id")),
        type(binding.get("revision")) is int,
        isinstance(binding.get("revision"), int) and binding["revision"] >= 0,
        re.fullmatch(r"[0-9a-f]{40}", head) is not None,
        head == pr["head"]["sha"], re.fullmatch(r"[0-9a-f]{64}", digest) is not None,
    ))


def readonly_coordinator_scope(
    gh: Any, pr: dict[str, Any], binding: dict[str, Any], *, stage: str,
) -> bool:
    """Permit only fresh read-only planning while its exact dependency stays held.

    This predicate processes no owner commands and changes no authority. It is
    never a coding, publication, hold-release, review or merge capability.
    """
    if stage not in {"plan", "plan-review", "plan-resolver"} or not _coordinator_target_valid(gh, pr, binding):
        return False
    with closing(connect()) as db, db:
        db.execute("BEGIN")
        rows = db.execute(
            "SELECT request,binding FROM coordinator_holds WHERE repository=? AND number=?",
            (gh.repository, pr["number"]),
        ).fetchall()
        if (len(rows) != 1 or rows[0]["request"] != binding["request_id"]
                or rows[0]["binding"] != json.dumps(binding, sort_keys=True)):
            return False
        pause, veto_row = _authority_records(db, gh.repository, pr["number"], pr["head"]["sha"])
        amendment = db.execute(
            "SELECT 1 FROM feature_amendments WHERE repository=? AND number=?",
            (gh.repository, pr["number"]),
        ).fetchone()
        return pause is None and veto_row is None and amendment is None


def replace_coordinator(
    gh: Any, pr: dict[str, Any], expected: dict[str, Any], replacement: dict[str, Any],
) -> bool:
    """CAS a reviewed inventory without briefly retiring its factual merge gate."""
    if not (_coordinator_target_valid(gh, pr, expected) and _coordinator_target_valid(gh, pr, replacement)):
        return False
    predecessor = hashlib.sha256(json.dumps(expected, sort_keys=True).encode()).hexdigest()
    if (expected["request_id"] != replacement["request_id"]
            or replacement["revision"] <= expected["revision"]
            or replacement.get("task_digest") != expected["task_digest"]
            or replacement.get("coordinator_predecessor_sha256") != predecessor):
        return False
    encoded = json.dumps(replacement, sort_keys=True)
    with closing(connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT binding FROM coordinator_holds WHERE repository=? AND number=? AND request=?",
            (gh.repository, pr["number"], expected["request_id"]),
        ).fetchone()
        if row is None:
            return False  # A retired hold can never be reinstalled by a replay.
        if row["binding"] == encoded:
            return True  # Exact predecessor identity binds a lost-response replay.
        if row["binding"] != json.dumps(expected, sort_keys=True):
            return False
        return bool(db.execute(
            "UPDATE coordinator_holds SET binding=? WHERE repository=? AND number=? AND request=? AND binding=?",
            (encoded, gh.repository, pr["number"], expected["request_id"], row["binding"]),
        ).rowcount)


def release_coordinator(gh: Any, pr: dict[str, Any], binding: dict[str, Any]) -> bool:
    """Caller verified the exact dependency receipt; retire only its binding."""
    with connect() as db:
        return bool(db.execute(
            "DELETE FROM coordinator_holds WHERE repository=? AND number=? "
            "AND request=? AND binding=?",
            (gh.repository, pr["number"], binding["request_id"],
             json.dumps(binding, sort_keys=True)),
        ).rowcount)


def renew_coordinator(
    gh: Any, pr: dict[str, Any], binding: dict[str, Any], digest: str,
) -> bool:
    """Atomically hand a verified dependency to renewed correction planning."""
    if (binding["repository"] != gh.repository or not digest or pr["state"] != "open"):
        return False
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        held = db.execute(
            "SELECT binding FROM coordinator_holds WHERE repository=? AND number=? AND request=?",
            (gh.repository, pr["number"], binding["request_id"]),
        ).fetchone()
        if db.execute("SELECT 1 FROM vetoes WHERE repository=? AND number=? AND head=?",
                      (gh.repository, pr["number"], pr["head"]["sha"])).fetchone():
            return False
        row = db.execute(
            "SELECT * FROM coordinator_renewals WHERE repository=? AND number=? AND request=?",
            (gh.repository, pr["number"], binding["request_id"]),
        ).fetchone()
        encoded = json.dumps(binding, sort_keys=True)
        if row is not None and row["binding"] == encoded:
            if row["head"] != pr["head"]["sha"] or row["digest"] != digest:
                return False
            # A lost response can replay this completed transition after restart,
            # even when intake has already installed a replacement dependency.
        else:
            if held is None or held["binding"] != encoded:
                return False
            if row is not None and json.loads(row["binding"])["revision"] > binding["revision"]:
                return False
            # Keep renewal authority separate from existing reviewer findings and
            # other requests. No earlier green review can authorize this head.
            db.execute("INSERT OR REPLACE INTO coordinator_renewals VALUES (?,?,?,?,?,?)",
                       (gh.repository, pr["number"], binding["request_id"], encoded,
                        pr["head"]["sha"], digest))
        db.execute(
            "DELETE FROM coordinator_holds WHERE repository=? AND number=? AND request=? AND binding=?",
            (gh.repository, pr["number"], binding["request_id"], encoded),
        )
    return True


def _authority_records(
    db: sqlite3.Connection, repository: str, number: int, head: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    pause_row = db.execute("SELECT * FROM pauses WHERE repository=? AND number=?",
                           (repository, number)).fetchone()
    veto_row = db.execute("SELECT * FROM vetoes WHERE repository=? AND number=? AND head=?",
                          (repository, number, head)).fetchone()
    return (dict(pause_row) if pause_row is not None else None,
            dict(veto_row) if veto_row is not None else None)


def authority_snapshot(
    repository: str, number: int, head: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Read both authorities, including absence, in one SQLite snapshot."""
    if not os.environ.get("AGENT_STATE_DIR"):
        return None, None
    with connect() as db:
        db.execute("BEGIN")
        return _authority_records(db, repository, number, head)


def recover_policy_comment(
    repository: str, number: int, head: str, created: float, *,
    expected: tuple[dict[str, Any] | None, dict[str, Any] | None],
) -> bool:
    """Retire only unchanged, strictly older owner authority; never clear holds."""
    if not os.environ.get("AGENT_STATE_DIR"):
        return expected == (None, None)
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        current = _authority_records(db, repository, number, head)
        if current != expected:
            return False
        row, denial = current
        if row is not None and not row["reason"].startswith("Owner disapproved"):
            return False
        if any(created <= record["created"] for record in current if record is not None):
            return False
        if row is not None:
            db.execute("DELETE FROM pauses WHERE repository=? AND number=?",
                       (repository, number))
        if denial is not None:
            db.execute("DELETE FROM vetoes WHERE repository=? AND number=? AND head=?",
                       (repository, number, head))
    return True


def paused(gh: Any, pr: dict[str, Any]) -> bool:
    # Owner resumes, approvals and derived-pause recovery cannot satisfy a
    # coordinator dependency. It also survives pushes and supervisor restart.
    if coordinator_held(gh, pr):
        return True
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        # A separate authority may have been cleared by a resume or verified
        # recovery. Transfer its pending amendment into the ordinary planning
        # pause without changing the unrelated hold's original audit fields.
        db.execute(
            "INSERT OR IGNORE INTO pauses "
            "SELECT repository,number,head,?,created FROM feature_amendments "
            "WHERE repository=? AND number=?",
            ("Owner changed the request; renewed planning is required.",
             gh.repository, pr["number"]))
        db.execute(
            "DELETE FROM feature_amendments WHERE repository=? AND number=? "
            "AND EXISTS (SELECT 1 FROM pauses WHERE pauses.repository=feature_amendments.repository "
            "AND pauses.number=feature_amendments.number AND pauses.head=feature_amendments.head "
            "AND pauses.created=feature_amendments.created AND pauses.reason=?)",
            (gh.repository, pr["number"], "Owner changed the request; renewed planning is required."))
        snapshot = _authority_records(db, gh.repository, pr["number"], pr["head"]["sha"])
    row, denial = snapshot
    declined = denial["created"] if denial is not None else 0
    if row is None:
        return False
    owner = gh.repository.split("/")[0].casefold()
    command = "/agent resume " + pr["head"]["sha"]
    comments = gh.pages(f"issues/{pr['number']}/comments?per_page=100")
    for item in comments:
        user = item.get("user", {})
        created = datetime.fromisoformat(item.get("created_at", "1970-01-01T00:00:00Z")
                                        .replace("Z", "+00:00")).timestamp()
        human = user.get("type") == "User" and user.get("login", "").casefold() == owner
        if (human and row["reason"].startswith("Owner disapproved")
                and item["body"].strip() == "/agent approve-policy " + pr["head"]["sha"]
                and created > max(row["created"], declined)):
            if not recover_policy_comment(
                    gh.repository, pr["number"], pr["head"]["sha"], created, expected=snapshot):
                return True  # Never refresh a failed authority comparison.
            return paused(gh, pr)  # Successful retirement may expose an amendment.
        if (human and item["body"].strip() == command and created > row["created"]):
            with connect() as db:
                retired = db.execute("DELETE FROM pauses WHERE repository=? AND number=? AND head=? AND reason=? AND created=?",
                                     (gh.repository, pr["number"], row["head"], row["reason"], row["created"])).rowcount
            # A failed comparison must not retry this comment against a replacement.
            if not retired or paused(gh, pr):
                return True
            _attention_display(gh, pr)
            return False
    return True


def approved(repository: str, number: int, head: str) -> bool:
    if not os.environ.get("AGENT_STATE_DIR"):
        return False
    with connect() as db:
        return db.execute("SELECT 1 FROM approvals WHERE repository=? AND number=? AND head=?",
                          (repository, number, head)).fetchone() is not None



def attention_snapshot(gh: Any, pr: dict[str, Any]) -> dict[str, Any] | None:
    """Observe a pause identity for display only; never consult owner comments."""
    with connect() as db:
        row = db.execute("SELECT * FROM pauses WHERE repository=? AND number=?",
                         (gh.repository, pr["number"])).fetchone()
    return dict(row) if row is not None else None


def attention_hint(gh: Any, pr: dict[str, Any], observed: dict[str, Any] | None,
                   required: bool, context: str) -> bool:
    """Host-only UI classification; cannot retire a pause or authorize any work."""
    if (type(required) is not bool or not re.fullmatch("[0-9a-f]{64}", context)
            or (observed is not None and (
                (not required and observed.get("head") != pr["head"]["sha"])
                or observed.get("reason") not in {"Correction agent requires user input.",
                    "Owner changed the request; renewed planning is required."}))):
        return False
    encoded = json.dumps(observed, sort_keys=True)
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM pauses WHERE repository=? AND number=?",
                         (gh.repository, pr["number"])).fetchone()
        if (dict(row) if row is not None else None) != observed:
            return False
        db.execute("INSERT OR REPLACE INTO attention_hints VALUES (?,?,?,?,?,?)",
                   (gh.repository, pr["number"], pr["head"]["sha"], encoded, required, context))
    return True


def owner_attention(gh: Any, pr: dict[str, Any]) -> bool:
    """Report actual owner attention independently of merge-blocking authority."""
    if not os.environ.get("AGENT_STATE_DIR"):
        return False  # Stateless CI/controller diagnostics have no private holds.
    with connect() as db:
        if db.execute("SELECT 1 FROM vetoes WHERE repository=? AND number=? AND head=?",
                      (gh.repository, pr["number"], pr["head"]["sha"])).fetchone():
            return True
        row = db.execute("SELECT * FROM pauses WHERE repository=? AND number=?",
                         (gh.repository, pr["number"])).fetchone()
        hint = db.execute("SELECT * FROM attention_hints WHERE repository=? AND number=?",
                          (gh.repository, pr["number"])).fetchone()
        if row is None:
            return bool(hint is not None and hint["head"] == pr["head"]["sha"]
                        and hint["pause"] == "null" and hint["required"])
        if row["reason"] not in {"Correction agent requires user input.",
                "Owner changed the request; renewed planning is required."}:
            return True  # Unknown owner/security/provenance reasons stay visible.
        if (hint is not None and hint["head"] == pr["head"]["sha"]
                and hint["pause"] == json.dumps(dict(row), sort_keys=True)):
            return bool(hint["required"])
        return row["reason"] != "Owner changed the request; renewed planning is required."


def display_work_pending(gh: Any, pr: dict[str, Any]) -> bool:
    """Read internal work gates for UI readiness, never process or clear them."""
    if not os.environ.get("AGENT_STATE_DIR"):
        return False
    repository, number, head = gh.repository, pr["number"], pr["head"]["sha"]
    with connect() as db:
        db.execute("BEGIN")  # One read snapshot spans amendment/dependency transfers.
        if db.execute("SELECT 1 FROM pauses WHERE repository=? AND number=?",
                      (repository, number)).fetchone():
            return True
        if db.execute("SELECT 1 FROM corrections WHERE repository=? AND number=? AND head=?",
                      (repository, number, head)).fetchone():
            return True
        # Additive coordinator APIs are independently delivered by the paired
        # recovery controller. Missing tables are legacy capability absence;
        # present malformed/unavailable tables are errors, never a false pass.
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, query, parameters in (
            ("coordinator_holds", "SELECT 1 FROM coordinator_holds WHERE repository=? AND number=?", (repository, number)),
            ("coordinator_renewals", "SELECT 1 FROM coordinator_renewals WHERE repository=? AND number=? AND head=?", (repository, number, head)),
            ("feature_amendments", "SELECT 1 FROM feature_amendments WHERE repository=? AND number=?", (repository, number)),
        ):
            if table in tables and db.execute(query, parameters).fetchone():
                return True
    return False


def reconcile_label(
    gh: Any, pr: dict[str, Any], *, approval_required: bool,
    approval_check: Callable[[dict[str, Any]], bool] | None = None,
) -> bool:
    """Recover authenticated decisions explicitly, then update display only.

    Legacy callers retain their head-bound bool. New controller/worker callers
    supply a pure current-readiness projection, never authenticated recovery.
    """
    number, head = pr["number"], pr["head"]["sha"]
    current = gh.pull(number)
    if current["state"] != "open" or current["head"]["sha"] != head:
        return False
    paused(gh, current)
    fresh = status_transition(gh, number, head, _preserve_phase,
        attention=approval_check if approval_check is not None else lambda latest: approval_required)
    if fresh is None:
        return False
    pr["labels"] = fresh["labels"]
    return True


def veto(repository: str, number: int, head: str) -> float:
    if not os.environ.get("AGENT_STATE_DIR"):
        return 0
    with connect() as db:
        row = db.execute("SELECT created FROM vetoes WHERE repository=? AND number=? AND head=?",
                         (repository, number, head)).fetchone()
    return row[0] if row else 0


def clear_owner_decline(gh: Any, number: int, head: str) -> None:
    with connect() as db:
        db.execute("DELETE FROM vetoes WHERE repository=? AND number=? AND head=?",
                   (gh.repository, number, head))
        row = db.execute("SELECT reason FROM pauses WHERE repository=? AND number=?",
                         (gh.repository, number)).fetchone()
        if row and row[0].startswith("Owner disapproved"):
            db.execute("DELETE FROM pauses WHERE repository=? AND number=?", (gh.repository, number))
    if row and row[0].startswith("Owner disapproved"):
        current = gh.pull(number)
        if current["head"]["sha"] == head:
            _attention_display(gh, current)


def assess(repository: str, number: int, head: str, digest: str, changes: str) -> None:
    if not changes.strip():
        raise ValueError("A concrete change assessment is required.")
    with connect() as db:
        db.execute("INSERT OR REPLACE INTO assessments VALUES (?,?,?,?,?)",
                   (repository, number, head, digest, changes))


def correction_snapshot(repository: str, number: int) -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute("SELECT * FROM corrections WHERE repository=? AND number=?",
                         (repository, number)).fetchone()
    return dict(row) if row else None


def require_correction(repository: str, number: int, head: str, digest: str,
                       reason: str, changes: str, *, expected: dict[str, Any] | None) -> bool:
    """Queue planning only if the observed scope has not changed; retain findings."""
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM corrections WHERE repository=? AND number=?",
                         (repository, number)).fetchone()
        current = dict(row) if row else None
        if current and current["head"] == head and current["digest"] == digest:
            return True
        if current != expected:
            return False
        db.execute("INSERT OR REPLACE INTO corrections VALUES (?,?,?,?,?,?,?)",
                   (repository, number, head, digest, reason, changes, time.time()))
    return True


def reject(gh: Any, pr: dict[str, Any], task: dict[str, Any], reason: str, changes: str) -> None:
    """Routine reviewer/CI rejection queues a new author, never an owner resume."""
    number, head = pr["number"], pr["head"]["sha"]
    with connect() as db:
        db.execute("INSERT OR REPLACE INTO corrections VALUES (?,?,?,?,?,?,?)",
                   (gh.repository, number, head, task["task_digest"], reason, changes, time.time()))
    marker = f"<!-- agent-correction-{head}-{task['task_digest']} -->"
    if not any(marker in item["body"] for item in gh.pages(f"issues/{number}/comments?per_page=100")):
        gh.comment(number, marker + "\nChanges requested.\n\nWhat changed: " + changes
                   + "\n\nWhy rejected:\n" + reason
                   + "\n\nA separate correction agent on RPi5 is queued. "
                   "Its fix is gated by the read-only fraud check and green fast CI.")
    enqueue(f"reject:{gh.repository}:{number}:{head}:{task['task_digest']}", "rejection",
            {"repository": gh.repository, "number": number, "head": head,
             "url": pr.get("html_url", f"https://github.com/{gh.repository}/pull/{number}"),
             "title": pr.get("title", "PR " + str(number)), "changes": changes,
             "reason": reason, "command": "A separate RPi5 correction agent is queued; no resume command is needed.",
             **_notification_metadata(pr, "review_rejection", task["task_digest"])})


def correction(repository: str, number: int, head: str, digest: str) -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute("SELECT reason,changes FROM corrections WHERE repository=? AND number=? AND head=? AND digest=?",
                         (repository, number, head, digest)).fetchone()
        if row is not None:
            return dict(row)
        renewal = db.execute(
            "SELECT 1 FROM coordinator_renewals WHERE repository=? AND number=? AND head=? AND digest=?",
            (repository, number, head, digest),
        ).fetchone()
    return ({"reason": "Verified coordinator dependency requires renewed planning and review.",
             "changes": "Preserve the complete request and revalidate its dependency evidence."}
            if renewal else None)


def hold_correction(gh: Any, pr: dict[str, Any], *, cancel: bool = False) -> None:
    """Private authenticated intake can wait for an answer without weakening vetoes."""
    with connect() as db:
        row = db.execute("SELECT reason FROM pauses WHERE repository=? AND number=?",
                         (gh.repository, pr["number"])).fetchone()
        if row and row["reason"] != "Correction agent requires user input.":
            raise ValueError("A separate owner/security hold is active.")
        db.execute("DELETE FROM attention_hints WHERE repository=? AND number=?",
                   (gh.repository, pr["number"]))
        db.execute("INSERT OR REPLACE INTO pauses VALUES (?,?,?,?,?)",
                   (gh.repository, pr["number"], pr["head"]["sha"],
                    "Owner cancelled the correction." if cancel else "Correction agent requires user input.", time.time()))
    _attention_display(gh, pr)


def _retire_derived_pause(gh: Any, pr: dict[str, Any], row: sqlite3.Row) -> None:
    """Retire the observed pause without erasing later authority decisions."""
    if pr["state"] != "open":
        return
    with connect() as db:
        db.execute(
            "DELETE FROM pauses WHERE repository=? AND number=? AND head=? AND reason=? AND created=? "
            "AND NOT EXISTS (SELECT 1 FROM vetoes WHERE repository=? AND number=? AND head=?)",
            (gh.repository, pr["number"], row["head"], row["reason"], row["created"],
             gh.repository, pr["number"], pr["head"]["sha"]),
        )
    # The exact-row/veto retirement above owns authority. Its completed result
    # is now observed separately for display, including replacement holds.
    _attention_display(gh, pr)


def resume_correction(gh: Any, pr: dict[str, Any]) -> None:
    with connect() as db:
        row = db.execute("SELECT * FROM pauses WHERE repository=? AND number=?",
                         (gh.repository, pr["number"])).fetchone()
    if row is None:
        _attention_display(gh, pr)  # Retry display after an interrupted completed retirement.
        return
    if row["reason"] != "Correction agent requires user input.":
        return
    _retire_derived_pause(gh, pr, row)


def approval_notice(gh: Any, pr: dict[str, Any], paths: list[str], task: dict[str, Any]) -> None:
    number, head = pr["number"], pr["head"]["sha"]
    from hashlib import sha256
    digest = sha256(json.dumps(task, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    with connect() as db:
        row = db.execute("SELECT changes FROM assessments WHERE repository=? AND number=? AND head=? AND digest=?",
                         (gh.repository, number, head, digest)).fetchone()
    if row is None:
        return  # Never ask the owner to approve an unassessed or stale candidate.
    enqueue(f"approval:{gh.repository}:{number}:{head}", "approval",
            {"repository": gh.repository, "number": number, "head": head,
             "url": pr.get("html_url", f"https://github.com/{gh.repository}/pull/{number}"),
             "title": pr.get("title", "PR " + str(number)),
             "changes": row["changes"][:2500], "paths": paths,
             "reason": "These files govern the controller, agent runtimes, their trusted prompts or "
             "security and merge permissions. Your retained policy requires owner approval "
             "for this exact head. Admission and fast CI are already green on it.",
             "command": "/agent approve-policy " + head,
             **_notification_metadata(pr, "owner_approval", digest)}, buttons=True)


def callback(gh: Any, query: dict[str, Any], owner_id: int, chat_id: int) -> str:
    """Called only by the authenticated host poller, never by a model tool."""
    message = query.get("message", {})
    if (query.get("from", {}).get("id") != owner_id
            or message.get("chat", {}).get("id") != chat_id):
        raise ValueError("Only the configured Telegram owner can decide.")
    action, separator, token = str(query.get("data", "")).partition(":")
    if separator != ":" or action not in {"approve", "disapprove"}:
        raise ValueError("Unsupported decision.")
    with connect() as db:
        row = db.execute("SELECT * FROM notifications WHERE token=? AND kind='approval'",
                         (token,)).fetchone()
    if row is None or row["message_id"] != message.get("message_id"):
        raise ValueError("Unknown approval message.")
    value = json.loads(row["payload"])
    if value["repository"] != gh.repository:
        raise ValueError("Wrong repository.")
    pr = gh.pull(value["number"])
    if pr["state"] != "open" or pr["head"]["sha"] != value["head"]:
        raise ValueError("PR changed. Use the approval message for its new head.")
    marker = f"<!-- telegram-owner-{query['id']} -->"
    comments = gh.pages(f"issues/{pr['number']}/comments?per_page=100")
    command = f"/agent {'approve-policy' if action == 'approve' else 'disapprove-policy'} {value['head']}"
    if not any(marker in item["body"] for item in comments):
        gh.comment(pr["number"], command + "\n\nOwner decision via Telegram.\n" + marker)
    with connect() as db:
        if action == "approve":
            db.execute("INSERT OR REPLACE INTO approvals VALUES (?,?,?,?,?)",
                       (gh.repository, pr["number"], value["head"], owner_id, query["id"]))
        else:
            db.execute("DELETE FROM approvals WHERE repository=? AND number=? AND head=?",
                       (gh.repository, pr["number"], value["head"]))
    if action == "disapprove":
        with connect() as db:
            db.execute("INSERT OR REPLACE INTO vetoes VALUES (?,?,?,?)",
                       (gh.repository, pr["number"], value["head"], time.time()))
        pause(gh, pr, "Owner disapproved the controller/security change via Telegram.",
              value["changes"])
    else:
        clear_owner_decline(gh, pr["number"], value["head"])
        reconcile_label(gh, pr, approval_required=False)
    return "Approved; green fast CI on this exact head still gates the merge." if action == "approve" else "Disapproved; PR paused."


def cursor(key: str, default: str | None = None) -> str | None:
    with connect() as db:
        row = db.execute("SELECT value FROM cursors WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_cursor(key: str, value: str) -> None:
    with connect() as db:
        db.execute("INSERT OR REPLACE INTO cursors VALUES (?,?)", (key, value))


def observe(gh: Any, app_id: int | None = None) -> None:
    """Notify confirmed events since activation; never replay historical releases."""
    prefix = gh.repository + ":notifications:"
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    start = cursor(prefix + "start")
    main = gh.api("git/ref/heads/main")["object"]["sha"]
    if start is None:
        set_cursor(prefix + "start", now)
        set_cursor(prefix + "main", main)
        return
    pulls = gh.pages("pulls?state=closed&base=dev&sort=updated&direction=desc&per_page=100")
    for pr in pulls:
        if not pr.get("merged_at") or pr["merged_at"] < start:
            continue
        key = f"merged:{gh.repository}:{pr['number']}:{pr['merge_commit_sha']}"
        if cursor(key):
            continue
        changes = pr["title"]
        if app_id is not None:
            reviews = [check for check in gh.checks(pr["head"]["sha"])
                       if check["name"] == "agent-review" and check.get("conclusion") == "success"
                       and check.get("app", {}).get("id") == app_id]
            if reviews:
                changes = reviews[0].get("output", {}).get("summary", changes).split("\n\n", 1)[0]
        enqueue(key, "merged",
                {"repository": gh.repository, "number": pr["number"],
                 "title": pr["title"], "changes": changes, "head": pr["merge_commit_sha"],
                 "url": pr["html_url"]})
        set_cursor(key, "sent-to-outbox")
    previous = cursor(prefix + "main", main)
    if previous != main:
        # The release workflow promotes dev itself. Automation-only main mirrors
        # are divergent commits and must not be described as dev promotions.
        ancestry = gh.api(f"compare/{main}...{gh.base()}")
        if ancestry["status"] in {"ahead", "identical"}:
            comparison = gh.api(f"compare/{previous}...{main}")
            titles = [item["commit"]["message"].splitlines()[0]
                      for item in comparison.get("commits", [])][-5:]
            enqueue(f"main:{gh.repository}:{main}", "main",
                    {"repository": gh.repository, "head": main, "title": "dev promoted to main",
                     "changes": "; ".join(titles),
                     "url": f"https://github.com/{gh.repository}/compare/{previous}...{main}"})
        set_cursor(prefix + "main", main)
    runs = gh.pages("actions/workflows/publish.yml/runs?status=success&event=release&per_page=100",
                    "workflow_runs")
    for run in runs:
        key = f"release:{gh.repository}:{run['id']}"
        if run.get("created_at", "") < start or cursor(key):
            continue
        jobs = gh.pages(f"actions/runs/{run['id']}/jobs?per_page=100", "jobs")
        if not any(job["name"] == "publish to PyPI" and job["conclusion"] == "success"
                   for job in jobs):
            continue
        releases = gh.pages("releases?per_page=100")
        for release in releases:
            if release.get("draft") or (release.get("published_at") or "") < start:
                continue
            commit = gh.api("commits/" + release["tag_name"])["sha"]
            if commit == run["head_sha"]:
                changes = release.get("body") or "Published package release."
                document = gh.api(f"contents/docs/CHANGELOG.md?ref={commit}")
                changelog = base64.b64decode(document["content"]).decode("utf-8")
                version = release["tag_name"][1:] if release["tag_name"].startswith("v") else release["tag_name"]
                section = changelog.split("## " + version + " ", 1)
                if len(section) == 2:
                    bullets = [line[2:] for line in section[1].split("\n## ", 1)[0].splitlines()
                               if line.startswith("- ")][:5]
                    if bullets:
                        changes = "; ".join(bullets)
                enqueue(f"release:{gh.repository}:{release['tag_name']}:{commit}", "release",
                        {"repository": gh.repository, "head": commit,
                         "title": release.get("name") or release["tag_name"],
                         "changes": changes[:1500],
                         "url": release["html_url"], "ci_url": run["html_url"]})
                set_cursor(key, "sent-to-outbox")
                break


def enroll_feature(repository: str, branch: str, digest: str) -> None:
    with connect() as db:
        db.execute("INSERT OR REPLACE INTO features VALUES (?,?,?)", (repository, branch, digest))
        db.execute("INSERT OR IGNORE INTO feature_contexts VALUES (?,?,?)", (repository, branch, digest))


def enrolled_feature(repository: str, branch: str, digest: str | None = None) -> bool:
    if not os.environ.get("AGENT_STATE_DIR"):
        return False
    with connect() as db:
        row = db.execute("SELECT digest FROM features WHERE repository=? AND branch=?", (repository, branch)).fetchone()
        if digest is not None:
            return db.execute("SELECT 1 FROM feature_contexts WHERE repository=? AND branch=? AND digest=?",
                              (repository, branch, digest)).fetchone() is not None
    return row is not None


def resume_feature(gh: Any, pr: dict[str, Any]) -> None:
    """Host-only authenticated owner response; never exposed as a model tool."""
    if not enrolled_feature(gh.repository, pr["head"]["ref"]):
        raise ValueError("Feature is not host-enrolled.")
    with connect() as db:
        db.execute("DELETE FROM pauses WHERE repository=? AND number=?", (gh.repository, pr["number"]))
    _attention_display(gh, pr)


def hold_feature(gh: Any, pr: dict[str, Any], *, cancel: bool = False) -> None:
    if not enrolled_feature(gh.repository, pr["head"]["ref"]):
        raise ValueError("Feature is not host-enrolled.")
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM attention_hints WHERE repository=? AND number=?",
                   (gh.repository, pr["number"]))
        row = db.execute("SELECT reason FROM pauses WHERE repository=? AND number=?",
                         (gh.repository, pr["number"])).fetchone()
        reason = "Owner changed the request; renewed planning is required."
        created = time.time()
        if not cancel and row is not None and row["reason"] != reason:
            # Record both authorities. A resume older than this amendment can
            # release the unrelated hold but cannot authorize the amended task.
            db.execute("INSERT OR REPLACE INTO feature_amendments VALUES (?,?,?,?)",
                       (gh.repository, pr["number"], pr["head"]["sha"], created))
        else:
            db.execute("INSERT OR REPLACE INTO pauses VALUES (?,?,?,?,?)",
                       (gh.repository, pr["number"], pr["head"]["sha"],
                        "Owner cancelled the request." if cancel else reason, created))
            if not cancel:
                db.execute("DELETE FROM feature_amendments WHERE repository=? AND number=?",
                           (gh.repository, pr["number"]))
    _attention_display(gh, pr)
    if cancel:
        gh.api(f"pulls/{pr['number']}", "PATCH", {"state": "closed"})


def recover_feature_publication(
    gh: Any,
    pr: dict[str, Any],
    *,
    task_digest: str | None = None,
) -> None:
    """Recover derived publication holds after caller verifies signed-head metadata."""
    with connect() as db:
        row = db.execute("SELECT * FROM pauses WHERE repository=? AND number=?",
                         (gh.repository, pr["number"])).fetchone()
    if row is None or pr["state"] != "open":
        return
    branch = pr["head"].get("ref", "")
    if row["reason"] == "PR task context is not bound to its signed head commit.":
        if not enrolled_feature(gh.repository, branch):
            raise ValueError("Feature is not host-enrolled.")
    elif not (row["reason"] == "Feature context does not match the host-owned request."
              and row["head"] == pr["head"]["sha"]
              and task_digest is not None and branch.startswith("codex/orchestrator-")
              and enrolled_feature(gh.repository, branch, task_digest)):
        return
    # The caller verified the artifact, context, plan and final signed metadata.
    # A later authority hold, even with identical text/head, is a different row.
    # Check vetoes in the same write so a decision arriving after verification wins.
    _retire_derived_pause(gh, pr, row)
