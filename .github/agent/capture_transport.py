"""Bounded fixed GitHub transport; immutable outcomes never imply execution.

This module cannot issue host authority. Its private mutation primitive is used
only after a trusted caller's live boundary (the remote bridge authenticates
the original App comment). Unresolved requests are discovery-only on restart.
"""

from __future__ import annotations

import base64
import os
import re
import sys
import time
import uuid
import http.client
import urllib.error
import urllib.request

# Python-I: resolve only this reviewed sibling, never candidate cwd/sys.path.
import importlib.util
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
REPOSITORY = "Karapsin/analytics_toolkit"
API_VERSION = "2022-11-28"
WORKFLOW = ".github/workflows/agent-visual-profile.yml"
OPERATIONS = {
    "production": "visual-profile-v2",
    "receiver-rehearsal": "visual-profile-rehearsal-v1",
}
ENDPOINTS = {"monitor-comment", "workflow-dispatch", "source-ref-create"}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _status(value):
    return value is None or type(value) is int and 100 <= value <= 599


def _positive(value):
    return D.integer(value)


def _hex(value, length=64):
    return (
        isinstance(value, str) and re.fullmatch("[0-9a-f]{" + str(length) + "}", value) is not None
    )


def _safe_int(headers, name, maximum=D.MAX_ID):
    text = headers.get(name)
    if text is None:
        return None
    if not re.fullmatch("[0-9]{1,16}", text) or int(text) > maximum:
        raise ValueError("Invalid bounded response header")
    return int(text)


class PrivateObservationPending(D.Pending):
    """Safe reference only; credential-bearing bytes never enter exception text."""

    def __init__(self, reference):
        super().__init__("Capture observation delivery pending; no accepted outcome")
        self.reference = reference


class HTTP:
    """No automatic redirects/retries. Time and bytes include all discovery."""

    def __init__(self, *, clock=time.monotonic, opener=None, private_root=None, budget=None):
        self.clock = clock
        self.budget = budget if budget is not None else D.Budget()
        self.private_root = private_root
        self.pending_observations = []
        self.deadline = clock() + 90
        self.remaining = 16 * 1024 * 1024
        self.opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect()
        )

    def request(self, path, method="GET", payload=None):
        if (
            method not in {"GET", "POST"}
            or not isinstance(path, str)
            or not re.fullmatch(
                r"(?:issues(?:/[0-9]+)?(?:/comments(?:/[0-9]+)?)?|pulls/[0-9]+|git/(?:refs|commits/[0-9a-f]{40}|ref/tags/agent-visual-source-v1-[0-9a-f]{40}|ref/heads/agent-visual/[0-9a-f]{40})|git/ref/heads/main|actions/(?:workflows/[A-Za-z0-9_.-]+(?:/runs|/dispatches)?|runs/[0-9]+(?:/attempts/[0-9]+(?:/jobs)?|/artifacts)?))(?:\?[A-Za-z0-9_=&:-]+)?",
                path,
            )
        ):
            raise ValueError("Unsupported fixed capture API path")
        if method == "POST" and not (
            re.fullmatch(r"issues/[0-9]+/comments", path)
            or path == "git/refs"
            or path == "actions/workflows/agent-visual-profile.yml/dispatches"
        ):
            raise ValueError("Unsupported capture mutation endpoint")
        token = os.environ.get("GH_TOKEN")
        if not token or os.environ.get("GITHUB_REPOSITORY", REPOSITORY) != REPOSITORY:
            raise D.Pending("Capture credential/repository binding unavailable")
        # Validate private original retention BEFORE a possibly applied POST.
        if self.private_root is None:
            runner = os.environ.get("RUNNER_TEMP")
            if runner is None:
                raise D.Pending("Private observation quarantine unavailable before request")
            self.private_root = Path(runner).resolve() / (
                "capture-private-http-" + uuid.uuid4().hex
            )
            self.private_root.mkdir(mode=0o700)
        with D.Store(self.private_root):
            pass
        maximum = 65536 if method == "POST" else 2097152
        if self.remaining <= 0 or self.clock() >= self.deadline:
            raise D.Pending("Capture metadata budget exhausted")
        request_body = None if payload is None else D.canonical(payload)
        if request_body is not None and (
            len(request_body) > 8192 or token.encode() in request_body
        ):
            raise ValueError("Unsafe capture request body")
        if request_body is not None:
            self.budget.charge(len(request_body), retained=True)
        # Hold the complete bounded header/body/finalization allowance BEFORE
        # opening the potentially applied request, after its actual payload cost.
        reservation = self.budget.reserve_response(min(maximum, self.remaining))
        limit = reservation.limit
        request = urllib.request.Request(
            "https://api.github.com/repos/" + REPOSITORY + "/" + path,
            data=request_body,
            method=method,
            headers={
                "Authorization": "Bearer " + token,
                "Accept": "application/vnd.github+json",
                "Accept-Encoding": "identity",
                "Content-Type": "application/json",
                "User-Agent": "atk-capture-profile-v2",
                "X-GitHub-Api-Version": API_VERSION,
            },
        )
        action_end = min(self.deadline, self.clock() + 20)
        status = None
        raw = bytearray()
        observed = 0
        complete_headers = complete_body = False
        error = "none"
        abnormal_exit = close_failed = False
        before_close_error = "none"
        declared = None
        header_bytes = 0
        safe = dict.fromkeys(
            (
                "request_id",
                "retry_after_seconds",
                "rate_limit_remaining",
                "rate_limit_reset_epoch",
                "content_type",
            )
        )
        response = None
        try:
            try:
                response = self.opener.open(
                    request, timeout=min(5, max(0.001, action_end - self.clock()))
                )
            except urllib.error.HTTPError as exc:
                response = exc
            status = response.status if hasattr(response, "status") else response.code
            if type(status) is not int or not 200 <= status <= 599:
                status = None
                raise ValueError("Invalid final HTTP status")
            headers = response.headers
            header_bytes = sum(len(k) + len(v) + 4 for k, v in headers.items())
            if header_bytes > 65536:
                raise ValueError("Response header limit")
            declared = _safe_int(headers, "Content-Length")
            safe.update(
                retry_after_seconds=_safe_int(headers, "Retry-After", 2147483647),
                rate_limit_remaining=_safe_int(headers, "X-RateLimit-Remaining", 2147483647),
                rate_limit_reset_epoch=_safe_int(headers, "X-RateLimit-Reset"),
            )
            request_id = headers.get("X-GitHub-Request-Id")
            content_type = headers.get("Content-Type")
            if request_id is not None and not re.fullmatch(r"[A-Za-z0-9:-]{0,256}", request_id):
                raise ValueError("Unsafe response header")
            if content_type is not None and not re.fullmatch(
                r"[A-Za-z0-9/;= ._+-]{0,128}", content_type
            ):
                raise ValueError("Unsafe response header")
            safe["request_id"], safe["content_type"] = request_id, content_type
            if headers.get("Content-Encoding", "identity") != "identity":
                raise ValueError("Unsupported response encoding")
            complete_headers = True
            while True:
                if self.clock() >= action_end:
                    raise TimeoutError
                # read(n) can wait for a whole chunk while bytes trickle; read1
                # returns available data so the absolute deadline is observed.
                block = response.read1(min(65536, limit + 1 - observed))
                if not block:
                    complete_body = declared is None or declared == observed
                    if not complete_body:
                        error = "body_incomplete"
                    break
                observed += len(block)
                raw.extend(block[: max(0, limit - len(raw))])
                if observed > limit or observed > self.remaining:
                    error = "body_limit"
                    break
            if 300 <= status < 400:
                error = "redirect_refused"
                complete_body = False
        except TimeoutError:
            error = "timeout_after_status" if status is not None else "timeout_before_status"
        except http.client.IncompleteRead as exc:
            observed += len(exc.partial)
            raw.extend(exc.partial[: max(0, limit - len(raw))])
            observed = min(observed, limit + 1)
            error = "body_incomplete"
        except urllib.error.URLError as exc:
            error = (
                ("timeout_after_status" if status is not None else "timeout_before_status")
                if isinstance(exc.reason, TimeoutError)
                else "connect_or_tls"
            )
        except (OSError, http.client.HTTPException):
            error = "io_error" if status is not None else "connect_or_tls"
        except (ValueError, AttributeError, OverflowError):
            error = "invalid_http"
        except BaseException:
            # Unexpected backend failures and interruption preserve the same
            # bounded observed prefix. Never echo exception text or accept it.
            abnormal_exit = True
            error = "invalid_http"
        finally:
            before_close_error = error
            if response is not None:
                try:
                    response.close()
                except BaseException:
                    # Cleanup cannot erase an already observed status/prefix or
                    # override retention by throwing out of this finalization.
                    abnormal_exit = close_failed = True
                    complete_body = False
                    error = "io_error"
        self.remaining -= observed
        if self.clock() >= action_end:
            complete_body = False
            error = "timeout_after_status" if status is not None else "timeout_before_status"
        facts = {
            "headers_complete": complete_headers,
            "body_complete": complete_body and error == "none",
            "captured_body_bytes": len(raw),
            "observed_body_bytes": observed,
            "declared_body_bytes": declared,
            "body_sha256": D.digest(bytes(raw)),
            "transport_error": error,
            "safe_headers": safe,
        }
        accounting_ok = False
        try:
            reservation.settle(len(raw), observed, min(header_bytes, 65536))
            accounting_ok = True
        except BaseException:
            # The original reservation stays charged on unexpected settlement
            # failure. Preserve actual observed identity as unavailable/pending;
            # never escape before safe diagnostics or invent accepted evidence.
            abnormal_exit = True
        tainted_headers = [
            key for key, value in safe.items() if isinstance(value, str) and token in value
        ]
        private = self.private_root
        if private is None:
            runner = os.environ.get("RUNNER_TEMP")
            if runner is None:
                raise D.Pending("Private observation quarantine unavailable")
            private = Path(runner).resolve() / ("capture-private-http-" + uuid.uuid4().hex)
            private.mkdir(mode=0o700)
        observation = {
            "http_status": status,
            "response_facts": facts,
            "body_sha256": D.digest(bytes(raw)),
            "body_bytes": len(raw),
            "abnormal_exit": abnormal_exit,
            "close_failed": close_failed,
            "before_close_error": before_close_error,
            "accounting_complete": accounting_ok,
        }
        identity = D.digest(D.canonical(observation))
        private_retained = False
        try:
            if not accounting_ok:
                raise D.Pending("Response accounting unavailable; originals not claimed")
            with D.Store(private, budget=self.budget) as quarantine:
                quarantine.put((identity, "body.bin"), bytes(raw))
                quarantine.put((identity, "observation.json"), D.canonical(observation))
            private_retained = True
        except BaseException:
            # Storage failure is an unavailable observation, never a claimed
            # durable original. Still deliver only safe observed identity facts.
            abnormal_exit = True
        if token.encode() in raw or tainted_headers or abnormal_exit:
            # No secret, original header value, or untrusted text is delivered.
            reference = {
                "schema_version": 1,
                "kind": "capture-profile-private-observation-v1",
                "http_status": status,
                "body_sha256": D.digest(bytes(raw)),
                "captured_body_bytes": len(raw),
                "observed_body_bytes": observed,
                "headers_complete": complete_headers,
                "body_complete": complete_body and error == "none",
                "tainted_header_names": tainted_headers,
                "private_observation_sha256": identity,
                "private_retained": private_retained,
                "abnormal_exit": abnormal_exit,
                "close_failed": close_failed,
                "before_close_error": before_close_error,
                "accounting_complete": accounting_ok,
                "delivery": "pending-private-only" if private_retained else "pending-unavailable",
                "state": "pending",
            }
            self.pending_observations.append(reference)
            raise PrivateObservationPending(reference)
        return status, bytes(raw), facts


def documented_rejection(endpoint, status, body, facts):
    """Conservative actual GitHub error families; no inferred rollback."""
    if (
        not facts["headers_complete"]
        or not facts["body_complete"]
        or facts["transport_error"] != "none"
    ):
        return False
    try:
        error = D.parse(body, 65536)
    except (ValueError, UnicodeError):
        return False
    if not isinstance(error, dict) or not isinstance(error.get("message"), str):
        return False
    if any(k in error for k in ("id", "workflow_run_id", "object", "ref")):
        return False
    message = error["message"].casefold()
    if status == 401:
        return message in {"bad credentials", "requires authentication"}
    if status == 403:
        return (
            message
            in {
                "forbidden",
                "resource not accessible by integration",
                "resource not accessible by personal access token",
            }
            or "rate limit" in message
            or "secondary rate" in message
        )
    if status == 404:
        return message == "not found"
    if status == 400:
        return message in {
            "problems parsing json",
            "body should be a json object",
            "not a supported version",
        }
    if status == 410:
        return (
            message == "not a supported version"
            or endpoint == "monitor-comment"
            and message == "gone"
        )
    if status == 422:
        return endpoint != "source-ref-create" and (
            message in {"validation failed", "invalid request"}
            or endpoint == "monitor-comment"
            and "spam" in message
        )
    if status == 429:
        return "rate limit" in message or facts["safe_headers"]["rate_limit_remaining"] == 0
    return False


def comment_metadata(value):
    """Total navigation-shape check; ordinary non-App comments are allowed."""
    if not isinstance(value, dict) or not isinstance(value.get("user"), dict):
        return False
    user, app = value["user"], value.get("performed_via_github_app")
    return (
        _positive(value.get("id"))
        and all(isinstance(value.get(k), str) for k in ("body", "url", "issue_url"))
        and _positive(user.get("id"))
        and all(isinstance(user.get(k), str) for k in ("type", "login"))
        and (
            app is None
            and user["id"] != 340139075
            or isinstance(app, dict)
            and _positive(app.get("id"))
            and isinstance(app.get("slug"), str)
        )
    )


def comment_identity(value, binding):
    if not comment_metadata(value):
        return False
    ident, user, app = value["id"], value["user"], value.get("performed_via_github_app")
    return (
        isinstance(app, dict)
        and value["issue_url"]
        == "https://api.github.com/repos/" + REPOSITORY + "/issues/" + str(binding["monitor_issue"])
        and value.get("url")
        == "https://api.github.com/repos/" + REPOSITORY + "/issues/comments/" + str(ident)
        and user["type"] == "Bot"
        and user["id"] == 340139075
        and user["login"] == "analytics-toolkit-agent-717959[bot]"
        and app["id"] == 5250620
        and app["slug"] == "analytics-toolkit-agent-717959"
        and D.digest(value["body"].encode()) == binding["comment_body_sha256"]
    )


def reference_metadata(value):
    if not isinstance(value, dict) or not isinstance(value.get("object"), dict):
        return False
    obj = value["object"]
    return (
        all(isinstance(value.get(k), str) for k in ("ref", "url"))
        and isinstance(obj.get("type"), str)
        and _hex(obj.get("sha"), 40)
        and ("url" not in obj or isinstance(obj["url"], str))
    )


def source_identity(value, binding):
    """Exact fixed response identity; never fetch any returned URL."""
    if not reference_metadata(value):
        return False
    obj = value["object"]
    prefix = "https://api.github.com/repos/" + REPOSITORY + "/git/"
    return (
        value["ref"] == "refs/tags/" + binding["source_tag"]
        and value["url"] == prefix + "refs/tags/" + binding["source_tag"]
        and obj["type"] == "commit"
        and obj["sha"] == binding["expected_trusted_head"]
        and (
            "url" not in obj or obj["url"] == prefix + "commits/" + binding["expected_trusted_head"]
        )
    )


def outcome(endpoint, status, body, facts, binding):
    """Classify original facts without guessed IDs or retrospective rewriting."""
    state = "uncertain"
    reason = (
        "possibly_applied_or_existing" if endpoint == "source-ref-create" else "possibly_applied"
    )
    ident = None
    complete = (
        facts["headers_complete"] and facts["body_complete"] and facts["transport_error"] == "none"
    )
    try:
        value = D.parse(body, 65536) if body else None
    except (ValueError, UnicodeError):
        value = None
    if complete:
        if endpoint == "monitor-comment" and status == 201 and comment_identity(value, binding):
            state, reason, ident = "posted", "complete_comment_identity", value["id"]
        elif endpoint == "workflow-dispatch":
            if status == 204 and not body and binding["return_run_details"] is False:
                state, reason = "accepted", "complete_empty_dispatch_acceptance"
            elif (
                status == 200
                and binding["return_run_details"] is True
                and isinstance(value, dict)
                and set(value) == {"workflow_run_id", "run_url", "html_url"}
                and _positive(value["workflow_run_id"])
                and value["run_url"]
                == "https://api.github.com/repos/"
                + REPOSITORY
                + "/actions/runs/"
                + str(value["workflow_run_id"])
                and value["html_url"]
                == "https://github.com/"
                + REPOSITORY
                + "/actions/runs/"
                + str(value["workflow_run_id"])
            ):
                state, reason, ident = (
                    "accepted",
                    "complete_dispatch_identity",
                    value["workflow_run_id"],
                )
        elif endpoint == "source-ref-create" and status == 201:
            if source_identity(value, binding):
                state, reason = "created_response", "complete_reference_identity"
        if state == "uncertain" and documented_rejection(endpoint, status, body, facts):
            state, reason = "rejected", "documented_api_rejection"
    return state, reason, ident


class Journal:
    """One durable POST per intent; reconciliation appends separate evidence."""

    def __init__(self, store, http):
        self.store, self.http = store, http
        self.budget = D.joint(http, store)

    def _parts(self, intent, attempt, name):
        if not _hex(intent, 32) or not _hex(attempt, 32):
            raise ValueError("Invalid capture attempt identity")
        return ("capture-transport", intent, attempt, name)

    def mutate(self, endpoint, path, payload, binding, *, intent_id, attempt_id):
        # The remote bridge root is run-owned; host journals use the persistent
        # private ledger. Repeated attempts in either root cannot race locally.
        with self.store.lease(("capture-transport", intent_id, "mutation.lock")):
            return self._mutate(
                endpoint, path, payload, binding, intent_id=intent_id, attempt_id=attempt_id
            )

    def _mutate(self, endpoint, path, payload, binding, *, intent_id, attempt_id):
        if endpoint not in ENDPOINTS:
            raise ValueError("Unsupported capture endpoint")
        validate_mutation(endpoint, path, payload, binding, intent_id, attempt_id)
        # Exact binding is part of durable intent; changed retries cannot adopt it.
        intent = {
            "endpoint": endpoint,
            "path": path,
            "payload": payload,
            "binding": binding,
            "intent_id": intent_id,
            "attempt_id": attempt_id,
            "api_version": API_VERSION,
            "begin_epoch_ms": int(time.time() * 1000),
            "begin_monotonic_ms": int(time.monotonic() * 1000),
        }
        # A repeated immutable intent reuses the originally observed time.
        try:
            previous = D.parse(self.store.read(("capture-transport", intent_id, "intent.json")))
        except FileNotFoundError:
            previous = None
        if previous is not None:
            for field in ("begin_epoch_ms", "begin_monotonic_ms"):
                intent[field] = previous[field]
        self.store.put(("capture-transport", intent_id, "intent.json"), D.canonical(intent))
        self.store.put(self._parts(intent_id, attempt_id, "intent.json"), D.canonical(intent))
        receipt_path = self._parts(intent_id, attempt_id, "receipt.json")
        try:
            return D.parse(self.store.read(receipt_path))
        except FileNotFoundError:
            pass
        started = self._parts(intent_id, attempt_id, "started.json")
        try:
            self.store.read(started)
        except FileNotFoundError:
            self.store.put(started, D.canonical({"intent_id": intent_id, "attempt_id": attempt_id}))
        else:
            raise D.Pending("Unresolved capture POST; exact discovery required")
        try:
            status, body, facts = self.http.request(path, "POST", payload)
        except PrivateObservationPending as exc:
            self.store.put(
                self._parts(intent_id, attempt_id, "delivery-pending.json"),
                D.canonical(exc.reference),
                emergency=True,
            )
            raise
        self.store.put(self._parts(intent_id, attempt_id, "body.bin"), body)
        facts["body_ref"] = {
            "intent_id": intent_id,
            "attempt_id": attempt_id,
            "sha256": D.digest(body),
            "bytes": len(body),
        }
        state, reason, resource = outcome(endpoint, status, body, facts, binding)
        kinds = {
            "monitor-comment": "capture-profile-monitor-post-v1",
            "workflow-dispatch": "capture-profile-Actions-dispatch-v1",
            "source-ref-create": "capture-profile-source-ref-create-v1",
        }
        receipt = dict(
            binding,
            schema_version=1,
            repository=REPOSITORY,
            kind=kinds[endpoint],
            intent_id=intent_id,
            attempt_id=attempt_id,
            api_version=API_VERSION,
            http_status=status,
            response_facts=facts,
            state=state,
            decision_basis=reason,
        )
        if endpoint == "monitor-comment":
            receipt["monitor_comment_id"] = resource
        elif endpoint == "workflow-dispatch":
            receipt["workflow_run_id"] = resource
        validate_receipt(receipt)
        self.store.put(receipt_path, D.canonical(receipt))
        return receipt

    def reconcile(
        self,
        receipt,
        endpoint,
        observations,
        verified_identity=None,
        *,
        complete=False,
        conflict=False,
    ):
        """Caller supplies inert actual GET observations, not a retry permission."""
        validate_receipt(receipt)
        if (
            endpoint not in ENDPOINTS
            or not isinstance(observations, list)
            or len(observations) > 500
        ):
            raise ValueError("Invalid bounded discovery")
        raw = D.canonical(observations)
        if len(raw) > 16 * 1024 * 1024:
            raise D.Pending("Capture discovery aggregate limit")
        # Identity must be reconstructed from complete actual endpoint evidence.
        matched = None
        if verified_identity is not None:
            candidates = [value for value in observations if isinstance(value, dict)]
            if endpoint == "monitor-comment":
                found = [v for v in candidates if comment_identity(v, receipt)]
                if len(found) == 1 and verified_identity == found[0]["id"]:
                    matched = {
                        "endpoint": endpoint,
                        "monitor_comment_id": found[0]["id"],
                        "comment_body_sha256": receipt["comment_body_sha256"],
                    }
            elif endpoint == "source-ref-create":
                found = [v for v in candidates if source_identity(v, receipt)]
                if len(found) == 1 and verified_identity == receipt["expected_trusted_head"]:
                    matched = {
                        "endpoint": endpoint,
                        "source_tag": receipt["source_tag"],
                        "commit": verified_identity,
                    }
            elif endpoint == "workflow-dispatch":
                found = [v for v in candidates if verified_run(v, receipt)]
                if len(found) == 1 and verified_identity == found[0]["run"]["id"]:
                    matched = {
                        "endpoint": endpoint,
                        "workflow_run_id": verified_identity,
                        "run_attempt": 1,
                        "workflow_id": receipt["workflow_id"],
                        "expected_trusted_head": receipt["expected_trusted_head"],
                    }
            if matched is None:
                raise D.Pending("Exact authenticated capture identity unavailable")
        prefix = self._parts(receipt["intent_id"], receipt["attempt_id"], D.digest(raw) + ".json")
        self.store.put(prefix, raw)
        if matched is not None:
            matched["identity_proof_sha256"] = D.digest(raw)
        record = {
            "schema_version": 1,
            "kind": "capture-profile-transport-reconciliation-v1",
            "repository": REPOSITORY,
            "intent_id": receipt["intent_id"],
            "attempt_id": receipt["attempt_id"],
            "original_receipt_sha256": D.digest(D.canonical(receipt)),
            "endpoint": endpoint,
            "discovery_index_sha256": D.digest(raw),
            "disposition": "conflict"
            if conflict
            else "matched"
            if matched
            else "not_found_in_complete_window"
            if complete
            else "incomplete",
            "verified_identity": None if conflict else matched,
        }
        self.store.put(
            self._parts(
                receipt["intent_id"], receipt["attempt_id"], D.digest(D.canonical(record)) + ".json"
            ),
            D.canonical(record),
        )
        return record

    def discover(self, receipt):
        """Bounded authenticated API navigation; never an automatic retry permit.

        Run identity needs the pinned immutable workflow and its actual completed
        preflight step, in addition to the derived nonce/comment/purpose title.
        Product/rehearsal results still require their complete independent review.
        """
        validate_receipt(receipt)
        endpoint = {
            "capture-profile-monitor-post-v1": "monitor-comment",
            "capture-profile-Actions-dispatch-v1": "workflow-dispatch",
            "capture-profile-source-ref-create-v1": "source-ref-create",
        }[receipt["kind"]]
        observations = []
        complete = False

        def get(path):
            status, raw, facts = self.http.request(path)
            observations.append(
                {
                    "path": path,
                    "http_status": status,
                    "raw_body": raw.decode("utf-8", errors="backslashreplace"),
                    "raw_body_base64": base64.b64encode(raw).decode("ascii"),
                    "facts": facts,
                }
            )
            if status != 200 or not facts["body_complete"]:
                raise D.Pending("Incomplete capture discovery metadata")
            return D.parse(raw)

        matches = []
        try:
            if endpoint == "source-ref-create":
                value = get("git/ref/tags/" + receipt["source_tag"])
                observations.append(value)
                if not reference_metadata(value):
                    raise D.Pending("Malformed capture source readback")
                if source_identity(value, receipt):
                    matches = [receipt["expected_trusted_head"]]
                elif value.get("ref"):
                    return self.reconcile(receipt, endpoint, observations, conflict=True)
                complete = True
            else:
                for page in range(1, 6):
                    path = (
                        ("issues/" + str(receipt["monitor_issue"]) + "/comments?")
                        if endpoint == "monitor-comment"
                        else "actions/workflows/agent-visual-profile.yml/runs?event=workflow_dispatch&"
                    )
                    value = get(path + "per_page=100&page=" + str(page))
                    rows = (
                        value
                        if endpoint == "monitor-comment"
                        else value.get("workflow_runs")
                        if isinstance(value, dict)
                        else None
                    )
                    if not isinstance(rows, list) or len(rows) > 100:
                        raise ValueError("Invalid complete capture discovery page")
                    for row in rows:
                        if endpoint == "monitor-comment":
                            if not comment_metadata(row):
                                raise D.Pending("Malformed capture comment navigation")
                            if comment_identity(row, receipt):
                                actual = get("issues/comments/" + str(row["id"]))
                                observations.append(actual)
                                if not comment_metadata(actual):
                                    raise D.Pending("Malformed capture comment readback")
                                if comment_identity(actual, receipt):
                                    matches.append(actual["id"])
                        elif _run_fields(row, receipt):
                            jobs = get(
                                "actions/runs/"
                                + str(row["id"])
                                + "/attempts/1/jobs?per_page=100&page=1"
                            )
                            identity = {
                                "kind": "capture-profile-run-discovery-v1",
                                "run": row,
                                "jobs": jobs,
                            }
                            observations.append(identity)
                            if verified_run(identity, receipt):
                                matches.append(row["id"])
                    if len(rows) < 100:
                        complete = True
                        break
        except (ValueError, OSError, D.Pending):
            return self.reconcile(receipt, endpoint, observations, complete=False)
        # Do not adopt even a matching run while the window is partial; a second
        # matching dispatch could be hidden on an unread page.
        return self.reconcile(
            receipt,
            endpoint,
            observations,
            matches[0] if complete and len(matches) == 1 else None,
            complete=complete and endpoint != "workflow-dispatch",
            conflict=len(matches) > 1,
        )


def _run_fields(value, receipt):
    return (
        isinstance(value, dict)
        and _positive(value.get("id"))
        and value.get("path") == WORKFLOW
        and _positive(value.get("workflow_id"))
        and value["workflow_id"] == receipt["workflow_id"]
        and value.get("head_sha") == receipt["expected_trusted_head"]
        and value.get("head_branch") == receipt["source_tag"]
        and value.get("event") == "workflow_dispatch"
        and type(value.get("run_attempt")) is int
        and value["run_attempt"] == 1
        and isinstance(value.get("repository"), dict)
        and value["repository"].get("full_name") == REPOSITORY
        and value.get("display_title")
        == "capture-receiver-v2 "
        + receipt["purpose"]
        + " "
        + str(receipt["monitor_comment_id"])
        + " "
        + receipt["nonce"]
    )


def verified_run(value, receipt):
    """Metadata navigation cannot authenticate exact envelope/time originals.

    Actual bounded receiver-artifact retrieval is not available in this unit.
    Keep positive resolution pending; neither a title nor a successful named
    step can fill that protected source capability.
    """
    if (
        isinstance(value, dict)
        and value.get("kind") == "capture-profile-run-discovery-v1"
        and _run_fields(value.get("run"), receipt)
    ):
        raise D.Pending("Exact authenticated receiver originals and intent interval unavailable")
    return False


def validate_mutation(endpoint, path, payload, binding, intent_id, attempt_id):
    """Reject malformed/mixed metadata BEFORE the first mutation."""
    kinds = {
        "monitor-comment": "capture-profile-monitor-post-v1",
        "workflow-dispatch": "capture-profile-Actions-dispatch-v1",
        "source-ref-create": "capture-profile-source-ref-create-v1",
    }
    facts = {
        "headers_complete": False,
        "body_complete": False,
        "captured_body_bytes": 0,
        "observed_body_bytes": 0,
        "declared_body_bytes": None,
        "body_sha256": D.digest(b""),
        "body_ref": {
            "intent_id": intent_id,
            "attempt_id": attempt_id,
            "sha256": D.digest(b""),
            "bytes": 0,
        },
        "transport_error": "not_sent",
        "safe_headers": dict.fromkeys(
            (
                "request_id",
                "retry_after_seconds",
                "rate_limit_remaining",
                "rate_limit_reset_epoch",
                "content_type",
            )
        ),
    }
    provisional = dict(
        binding,
        schema_version=1,
        repository=REPOSITORY,
        kind=kinds[endpoint],
        intent_id=intent_id,
        attempt_id=attempt_id,
        api_version=API_VERSION,
        http_status=None,
        response_facts=facts,
        state="uncertain",
        decision_basis="possibly_applied_or_existing"
        if endpoint == "source-ref-create"
        else "possibly_applied",
    )
    if endpoint == "monitor-comment":
        provisional["monitor_comment_id"] = None
    elif endpoint == "workflow-dispatch":
        provisional["workflow_run_id"] = None
    validate_receipt(provisional)
    if not isinstance(payload, dict):
        raise ValueError("Invalid capture mutation body")
    if endpoint == "source-ref-create":
        if path != "git/refs" or payload != {
            "ref": "refs/tags/" + binding["source_tag"],
            "sha": binding["expected_trusted_head"],
        }:
            raise ValueError("Invalid create-only capture source mutation")
        return
    if endpoint == "monitor-comment":
        if (
            path != "issues/" + str(binding["monitor_issue"]) + "/comments"
            or set(payload) != {"body"}
            or not isinstance(payload["body"], str)
        ):
            raise ValueError("Invalid capture monitor mutation")
        body = payload["body"].encode()
        prefix = b"<!-- github-agent-dispatch\n"
        if (
            len(body) > 4096
            or not body.startswith(prefix)
            or not body.endswith(b"\n-->")
            or D.digest(body) != binding["comment_body_sha256"]
        ):
            raise ValueError("Invalid canonical capture monitor body")
        task = D.parse(body[len(prefix) : -4], 4096)
        if (
            not isinstance(task, dict)
            or set(task) != {"operation", "value"}
            or task["operation"] != binding["operation"]
        ):
            raise ValueError("Mixed capture monitor operation")
        envelope = task["value"]
    else:
        if path != "actions/workflows/agent-visual-profile.yml/dispatches" or set(payload) != {
            "ref",
            "return_run_details",
            "inputs",
        }:
            raise ValueError("Invalid fixed capture dispatch mutation")
        inputs = payload["inputs"]
        if (
            not isinstance(inputs, dict)
            or set(inputs)
            != {"protocol", "purpose", "expected_trusted_head", "monitor_comment_id", "envelope"}
            or inputs["protocol"] != "capture-receiver-v2"
            or inputs["purpose"] != binding["purpose"]
            or inputs["expected_trusted_head"] != binding["expected_trusted_head"]
            or inputs["monitor_comment_id"] != str(binding["monitor_comment_id"])
            or payload["ref"] != binding["source_tag"]
            or payload["return_run_details"] is not binding["return_run_details"]
            or not isinstance(inputs["envelope"], str)
        ):
            raise ValueError("Mixed capture dispatch source/purpose")
        raw = inputs["envelope"].encode()
        envelope = D.parse(raw, 4096)
        if raw != D.canonical(envelope):
            raise ValueError("Noncanonical capture dispatch input")
    contract = D.sibling("capture_receiver").schema()
    if (
        not D.matches(envelope, contract, contract["$defs"])
        or envelope["purpose"] != binding["purpose"]
        or envelope["operation"] != binding["operation"]
        or envelope["attempt"]["nonce"] != binding["nonce"]
        or D.digest(D.canonical(envelope)) != binding["envelope_sha256"]
    ):
        raise ValueError("Unbound capture mutation envelope")
    if endpoint == "workflow-dispatch" and (
        envelope["source"]["expected_trusted_head"] != binding["expected_trusted_head"]
        or envelope["source"]["source_tag"] != binding["source_tag"]
    ):
        raise ValueError("Unbound capture dispatch source")
    if (
        endpoint == "monitor-comment"
        and envelope["authorization"]["admission_receipt_sha256"]
        != binding["admission_receipt_sha256"]
    ):
        raise ValueError("Unbound capture monitor admission")


def validate_receipt(value):
    """Closed observed transport record, including status/state coherence."""
    common = {
        "schema_version",
        "repository",
        "intent_id",
        "attempt_id",
        "api_version",
        "http_status",
        "response_facts",
        "kind",
        "state",
        "decision_basis",
        "purpose",
    }
    extras = {
        "capture-profile-monitor-post-v1": {
            "operation",
            "nonce",
            "envelope_sha256",
            "admission_receipt_sha256",
            "monitor_issue",
            "monitor_comment_id",
            "comment_body_sha256",
        },
        "capture-profile-Actions-dispatch-v1": {
            "operation",
            "nonce",
            "envelope_sha256",
            "monitor_comment_id",
            "workflow_id",
            "workflow_path",
            "source_tag",
            "expected_trusted_head",
            "bridge_run_id",
            "bridge_run_attempt",
            "return_run_details",
            "workflow_run_id",
        },
        "capture-profile-source-ref-create-v1": {
            "operation_id",
            "deployment_intent_sha256",
            "source_tag",
            "expected_trusted_head",
        },
    }
    if (
        not isinstance(value, dict)
        or value.get("kind") not in extras
        or set(value) != common | extras[value["kind"]]
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["repository"] != REPOSITORY
        or value["api_version"] != API_VERSION
        or not _status(value["http_status"])
    ):
        raise ValueError("Invalid capture transport receipt")
    for key in ("intent_id", "attempt_id", "nonce", "operation_id"):
        if key in value and not _hex(value[key], 32):
            raise ValueError("Invalid capture transport identity")
    for key in (
        "envelope_sha256",
        "admission_receipt_sha256",
        "comment_body_sha256",
        "deployment_intent_sha256",
    ):
        if key in value and not _hex(value[key]):
            raise ValueError("Invalid capture transport digest")
    facts = value["response_facts"]
    exact_facts = {
        "headers_complete",
        "body_complete",
        "captured_body_bytes",
        "observed_body_bytes",
        "declared_body_bytes",
        "body_ref",
        "body_sha256",
        "transport_error",
        "safe_headers",
    }
    if not isinstance(facts, dict) or set(facts) != exact_facts:
        raise ValueError("Invalid capture response facts")
    if any(type(facts[k]) is not bool for k in ("headers_complete", "body_complete")):
        raise ValueError("Invalid capture response completeness")
    captured, observed = facts["captured_body_bytes"], facts["observed_body_bytes"]
    if (
        not D.integer(captured, 0)
        or not D.integer(observed, 0)
        or not 0 <= captured <= 65536
        or not captured <= observed <= 65537
    ):
        raise ValueError("Invalid capture response count")
    body_ref = facts["body_ref"]
    if (
        not isinstance(body_ref, dict)
        or set(body_ref) != {"intent_id", "attempt_id", "sha256", "bytes"}
        or type(body_ref["bytes"]) is not int
    ):
        raise ValueError("Invalid strict response body binding")
    if body_ref != {
        "intent_id": value["intent_id"],
        "attempt_id": value["attempt_id"],
        "sha256": facts["body_sha256"],
        "bytes": captured,
    } or not _hex(facts["body_sha256"]):
        raise ValueError("Invalid response body binding")
    if facts["declared_body_bytes"] is not None and not D.integer(facts["declared_body_bytes"], 0):
        raise ValueError("Invalid declared response count")
    if facts["transport_error"] not in {
        "none",
        "not_sent",
        "connect_or_tls",
        "timeout_before_status",
        "timeout_after_status",
        "body_incomplete",
        "body_limit",
        "invalid_http",
        "redirect_refused",
        "io_error",
    }:
        raise ValueError("Invalid capture transport error")
    headers = facts["safe_headers"]
    if not isinstance(headers, dict) or set(headers) != {
        "request_id",
        "retry_after_seconds",
        "rate_limit_remaining",
        "rate_limit_reset_epoch",
        "content_type",
    }:
        raise ValueError("Invalid safe response headers")
    for key in ("retry_after_seconds", "rate_limit_remaining", "rate_limit_reset_epoch"):
        maximum = D.MAX_ID if key == "rate_limit_reset_epoch" else 2147483647
        if headers[key] is not None and (not D.integer(headers[key], 0) or headers[key] > maximum):
            raise ValueError("Invalid safe response header count")
    for key, pattern in (
        ("request_id", r"[A-Za-z0-9:-]{0,256}"),
        ("content_type", r"[A-Za-z0-9/;= ._+-]{0,128}"),
    ):
        if headers[key] is not None and (
            not isinstance(headers[key], str) or re.fullmatch(pattern, headers[key]) is None
        ):
            raise ValueError("Invalid safe response header value")
    complete = (
        facts["headers_complete"] and facts["body_complete"] and facts["transport_error"] == "none"
    )
    if facts["body_complete"] and (
        not complete or observed != captured or value["http_status"] is None
    ):
        raise ValueError("False complete capture response")
    if (
        facts["body_complete"]
        and facts["declared_body_bytes"] is not None
        and facts["declared_body_bytes"] != observed
    ):
        raise ValueError("Incomplete declared response body")
    if facts["headers_complete"] and value["http_status"] is None:
        raise ValueError("Complete headers without final status")
    source = value["kind"] == "capture-profile-source-ref-create-v1"
    if source:
        if value["purpose"] != "source-deployment":
            raise ValueError("Invalid source deployment purpose")
    elif OPERATIONS.get(value["purpose"]) != value["operation"]:
        raise ValueError("Mixed capture transport purpose")
    if value["kind"] == "capture-profile-Actions-dispatch-v1" and (
        value["workflow_path"] != WORKFLOW
        or type(value["bridge_run_attempt"]) is not int
        or value["bridge_run_attempt"] != 1
        or not _positive(value["monitor_comment_id"])
        or type(value["return_run_details"]) is not bool
    ):
        raise ValueError("Invalid actual dispatch binding")
    for key in ("monitor_issue", "workflow_id", "bridge_run_id"):
        if key in value and not _positive(value[key]):
            raise ValueError("Invalid actual capture ID")
    if "source_tag" in value and (
        not _hex(value["expected_trusted_head"], 40)
        or value["source_tag"] != "agent-visual-source-v1-" + value["expected_trusted_head"]
    ):
        raise ValueError("Invalid pinned capture source")
    state = value["state"]
    if state == "uncertain":
        if value["decision_basis"] != (
            "possibly_applied_or_existing" if source else "possibly_applied"
        ):
            raise ValueError("Invalid uncertain capture reason")
        if any(
            value.get(k) is not None
            for k in ("workflow_run_id", "monitor_comment_id")
            if k == "workflow_run_id" or value["kind"] == "capture-profile-monitor-post-v1"
        ):
            raise ValueError("Guessed uncertain capture identity")
    elif state == "rejected":
        allowed = {400, 401, 403, 404, 410, 429} | (set() if source else {422})
        if (
            not complete
            or value["http_status"] not in allowed
            or value["decision_basis"] != "documented_api_rejection"
        ):
            raise ValueError("Invalid rejected capture outcome")
        if (
            value["kind"] == "capture-profile-monitor-post-v1"
            and value["monitor_comment_id"] is not None
            or "workflow_run_id" in value
            and value["workflow_run_id"] is not None
        ):
            raise ValueError("Rejected capture has a guessed identity")
    elif state in {"posted", "created_response"}:
        expected_kind = (
            "capture-profile-monitor-post-v1"
            if state == "posted"
            else "capture-profile-source-ref-create-v1"
        )
        expected_reason = (
            "complete_comment_identity" if state == "posted" else "complete_reference_identity"
        )
        if (
            value["kind"] != expected_kind
            or value["decision_basis"] != expected_reason
            or not complete
            or value["http_status"] != 201
            or (state == "posted" and not _positive(value.get("monitor_comment_id")))
        ):
            raise ValueError("Invalid created capture outcome")
    elif state == "accepted":
        if not complete or value["kind"] != "capture-profile-Actions-dispatch-v1":
            raise ValueError("Invalid accepted capture outcome")
        if value["http_status"] == 204:
            if (
                value["decision_basis"] != "complete_empty_dispatch_acceptance"
                or value["return_run_details"] is not False
                or value["workflow_run_id"] is not None
                or observed
            ):
                raise ValueError("Invalid empty dispatch acceptance")
        elif (
            value["decision_basis"] != "complete_dispatch_identity"
            or value["http_status"] != 200
            or value["return_run_details"] is not True
            or not _positive(value["workflow_run_id"])
        ):
            raise ValueError("Invalid dispatch identity")
    else:
        raise ValueError("Invalid capture outcome state")
    return value
