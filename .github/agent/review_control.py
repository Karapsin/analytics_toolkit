"""Host-owned pauses, owner decisions and a durable notification outbox."""
from __future__ import annotations

import json
import base64
import os
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PAUSED = "agent:needs-user"


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
        db.execute("INSERT OR IGNORE INTO notifications(key,kind,payload,token,sent) VALUES (?,?,?,?,?)",
                   (key, kind, json.dumps(payload), secrets.token_urlsafe(18) if buttons else None,
                    0 if notify(kind, payload) else -1))


def notify(kind: str, payload: dict[str, Any]) -> bool:
    return (kind in {"approval", "request-auth", "merged", "main", "release"}
            or kind == "request" and (payload.get("telegram_feature") is True or payload.get("informational") is True))


def pending() -> list[dict[str, Any]]:
    with connect() as db:
        # Retire obsolete backlog too, including undelivered multipart alerts.
        for row in db.execute("SELECT key,kind,payload FROM notifications WHERE sent=0").fetchall():
            if not notify(row["kind"], json.loads(row["payload"])):
                db.execute("UPDATE notifications SET sent=-1 WHERE key=?", (row["key"],))
        return [dict(row) for row in db.execute(
            "SELECT * FROM notifications WHERE sent=0 AND retry<=? ORDER BY rowid LIMIT 20",
            (time.time(),))]


def delivery(key: str, *, message_id: int | None = None, delay: float = 0) -> None:
    with connect() as db:
        if message_id is not None:
            db.execute("UPDATE notifications SET sent=1,message_id=? WHERE key=?", (message_id, key))
        else:
            db.execute("UPDATE notifications SET retry=? WHERE key=?", (time.time() + delay, key))


def pause(gh: Any, pr: dict[str, Any], reason: str, changes: str) -> None:
    head, number = pr["head"]["sha"], pr["number"]
    with connect() as db:
        db.execute("INSERT OR IGNORE INTO pauses VALUES (?,?,?,?,?)",
                   (gh.repository, number, head, reason, time.time()))
    gh.api(f"issues/{number}/labels", "POST", {"labels": [PAUSED]})
    marker = f"<!-- agent-rejection-{head} -->"
    comments = gh.pages(f"issues/{number}/comments?per_page=100")
    owner = gh.repository.split("/")[0]
    if not any(marker in item["body"] for item in comments):
        gh.comment(number, marker + f"\n@{owner} This PR requires user input.\n\n"
                   + "What changed: " + changes + "\n\nWhy rejected:\n" + reason
                   + "\n\nAutomatic corrections are paused. To resume, post exactly:\n"
                   + f"`/agent resume {head}`")
    enqueue(f"reject:{gh.repository}:{number}:{head}", "rejection",
            {"repository": gh.repository, "number": number, "head": head,
             "url": pr.get("html_url", f"https://github.com/{gh.repository}/pull/{number}"),
             "title": pr.get("title", "PR " + str(number)), "changes": changes, "reason": reason, "command": ("/reply_agent " + pr["head"]["ref"][len("codex/telegram-feature-"):].split("-", 1)[0] + " <correction>"
                if pr["head"].get("ref", "").startswith("codex/telegram-feature-") else "/agent resume " + head)})


def paused(gh: Any, pr: dict[str, Any]) -> bool:
    with connect() as db:
        row = db.execute("SELECT * FROM pauses WHERE repository=? AND number=?",
                         (gh.repository, pr["number"])).fetchone()
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
                and created > row["created"]):
            clear_owner_decline(gh, pr["number"], pr["head"]["sha"])
            return False
        if (human and item["body"].strip() == command and created > row["created"]):
            with connect() as db:
                db.execute("DELETE FROM pauses WHERE repository=? AND number=?",
                           (gh.repository, pr["number"]))
            labels = gh.api(f"issues/{pr['number']}/labels")
            if any(label["name"] == PAUSED for label in labels):
                gh.api(f"issues/{pr['number']}/labels/{PAUSED}", "DELETE")
            return False
    return True


def approved(repository: str, number: int, head: str) -> bool:
    if not os.environ.get("AGENT_STATE_DIR"):
        return False
    with connect() as db:
        return db.execute("SELECT 1 FROM approvals WHERE repository=? AND number=? AND head=?",
                          (repository, number, head)).fetchone() is not None


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
        labels = gh.api(f"issues/{number}/labels")
        if any(label["name"] == PAUSED for label in labels):
            gh.api(f"issues/{number}/labels/{PAUSED}", "DELETE")


def assess(repository: str, number: int, head: str, digest: str, changes: str) -> None:
    if not changes.strip():
        raise ValueError("A concrete change assessment is required.")
    with connect() as db:
        db.execute("INSERT OR REPLACE INTO assessments VALUES (?,?,?,?,?)",
                   (repository, number, head, digest, changes))


def reject(gh: Any, pr: dict[str, Any], task: dict[str, Any], reason: str, changes: str) -> None:
    """Routine reviewer/CI rejection queues a new author, never an owner resume."""
    number, head = pr["number"], pr["head"]["sha"]
    with connect() as db:
        db.execute("INSERT OR REPLACE INTO corrections VALUES (?,?,?,?,?,?,?)",
                   (gh.repository, number, head, task["task_digest"], reason, changes, time.time()))
    marker = f"<!-- agent-correction-{head}-{task['task_digest']} -->"
    if not any(marker in item["body"] for item in gh.pages(f"issues/{number}/comments?per_page=100")):
        gh.comment(number, marker + "\nReviewer requested changes.\n\nWhat changed: " + changes
                   + "\n\nWhy rejected:\n" + reason
                   + "\n\nA separate correction agent on RPi5 is queued. "
                   "It will plan the correction and submit it for independent review.")
    enqueue(f"reject:{gh.repository}:{number}:{head}:{task['task_digest']}", "rejection",
            {"repository": gh.repository, "number": number, "head": head,
             "url": pr.get("html_url", f"https://github.com/{gh.repository}/pull/{number}"),
             "title": pr.get("title", "PR " + str(number)), "changes": changes,
             "reason": reason, "command": "A separate RPi5 correction agent is queued; no resume command is needed."})


def correction(repository: str, number: int, head: str, digest: str) -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute("SELECT reason,changes FROM corrections WHERE repository=? AND number=? AND head=? AND digest=?",
                         (repository, number, head, digest)).fetchone()
    return dict(row) if row else None


def hold_correction(gh: Any, pr: dict[str, Any], *, cancel: bool = False) -> None:
    """Private authenticated intake can wait for an answer without weakening vetoes."""
    with connect() as db:
        row = db.execute("SELECT reason FROM pauses WHERE repository=? AND number=?",
                         (gh.repository, pr["number"])).fetchone()
        if row and row["reason"] != "Correction agent requires user input.":
            raise ValueError("A separate owner/security hold is active.")
        db.execute("INSERT OR REPLACE INTO pauses VALUES (?,?,?,?,?)",
                   (gh.repository, pr["number"], pr["head"]["sha"],
                    "Owner cancelled the correction." if cancel else "Correction agent requires user input.", time.time()))
    gh.api(f"issues/{pr['number']}/labels", "POST", {"labels": [PAUSED]})


def resume_correction(gh: Any, pr: dict[str, Any]) -> None:
    with connect() as db:
        row = db.execute("SELECT reason FROM pauses WHERE repository=? AND number=?",
                         (gh.repository, pr["number"])).fetchone()
        if not row or row["reason"] != "Correction agent requires user input.":
            return
        db.execute("DELETE FROM pauses WHERE repository=? AND number=?", (gh.repository, pr["number"]))
    labels = gh.api(f"issues/{pr['number']}/labels")
    if any(item["name"] == PAUSED for item in labels):
        gh.api(f"issues/{pr['number']}/labels/{PAUSED}", "DELETE")


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
             "reason": "These files govern the reviewer/controller, its trusted prompts or "
             "security and merge permissions. Your retained policy requires owner approval "
             "for this exact head, in addition to separate review and fast CI.",
             "command": "/agent approve-policy " + head}, buttons=True)


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
    return "Approved; separate review and fast CI still apply." if action == "approve" else "Disapproved; PR paused."


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
    labels = gh.api(f"issues/{pr['number']}/labels")
    if any(item["name"] == PAUSED for item in labels):
        gh.api(f"issues/{pr['number']}/labels/{PAUSED}", "DELETE")


def hold_feature(gh: Any, pr: dict[str, Any], *, cancel: bool = False) -> None:
    if not enrolled_feature(gh.repository, pr["head"]["ref"]):
        raise ValueError("Feature is not host-enrolled.")
    with connect() as db:
        db.execute("INSERT OR REPLACE INTO pauses VALUES (?,?,?,?,?)",
                   (gh.repository, pr["number"], pr["head"]["sha"],
                    "Owner cancelled the request." if cancel else "Owner changed the request; renewed planning is required.",
                    time.time()))
    gh.api(f"issues/{pr['number']}/labels", "POST", {"labels": [PAUSED]})
    if cancel:
        gh.api(f"pulls/{pr['number']}", "PATCH", {"state": "closed"})


def recover_feature_publication(gh: Any, pr: dict[str, Any]) -> None:
    """Clear only a known interrupted body-update hold after signed-head recovery."""
    with connect() as db:
        row = db.execute("SELECT reason FROM pauses WHERE repository=? AND number=?",
                         (gh.repository, pr["number"])).fetchone()
    if row is not None and row[0] == "PR task context is not bound to its signed head commit.":
        resume_feature(gh, pr)
