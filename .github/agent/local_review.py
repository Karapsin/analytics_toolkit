"""Signed local review transport; host-owned enrollment remains authoritative."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

MARKER = "atk-local-review:v1"
NAMESPACE = "atk-local-review-v1"
LIMIT = 48 * 1024
RUNTIMES = frozenset({"codex", "claude-code"})


def canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def attachment(receipt: dict[str, Any]) -> str:
    data = canonical(receipt)
    if len(data) > LIMIT:
        raise ValueError("Local review receipt exceeds its transport limit")
    return "<!-- " + MARKER + "\n" + base64.b64encode(data).decode() + "\n-->"


def extract(body: str) -> dict[str, Any] | None:
    matches = re.findall(r"<!-- " + re.escape(MARKER) + r"\n([^\n]+)\n-->", body)
    if not matches:
        if MARKER in body:
            raise ValueError("Malformed local review receipt")
        return None
    if len(matches) != 1 or len(matches[0]) > LIMIT * 2:
        raise ValueError("Ambiguous or oversized local review receipt")
    data = base64.b64decode(matches[0], validate=True)
    value = json.loads(data)
    if len(data) > LIMIT or not isinstance(value, dict) or canonical(value) != data:
        raise ValueError("Local review receipt is not canonical")
    return value


def validate_binding(receipt: dict[str, Any], *, repository: str, head: str, base: str,
                     tree: str, task_digest: str, producer_id: str) -> dict[str, Any]:
    payload = receipt.get("payload")
    expected = dict(repository=repository, head=head, base=base, tree=tree,
                    task_digest=task_digest, producer_id=producer_id)
    if (not isinstance(payload, dict) or payload.get("version") != 1
            or payload.get("kind") != "local-source-review"
            or payload.get("mode") != "read-only"
            or any(payload.get(key) != value for key, value in expected.items())
            or payload.get("approved") is not True or payload.get("alignment") != "aligned"
            or not re.fullmatch(r"reviewer:[0-9a-f-]{36}", str(payload.get("reviewer_id", "")))
            or payload["reviewer_id"] == producer_id
            or not re.fullmatch(r"[0-9a-f]{64}", str(payload.get("execution_sha256", "")))
            or not isinstance(payload.get("summary"), str) or not payload["summary"].strip()
            or not isinstance(receipt.get("signature"), str)):
        raise ValueError("Local review does not approve this exact source, task and base")
    # Claude/Codex parity: an absent runtime is a legacy Codex receipt.
    if (any(not isinstance(value, str) for key, value in payload.items() if key.startswith("reviewer_"))
            or payload.get("reviewer_runtime", "codex") not in RUNTIMES):
        raise ValueError("Local review names an unsupported reviewer runtime")
    return payload


def verify_signature(receipt: dict[str, Any], allowed_signers: Path) -> None:
    """Only the private host registry can enroll or revoke a review signer."""
    with tempfile.TemporaryDirectory(prefix="atk-review-signature-") as directory:
        signature = Path(directory) / "receipt.sig"
        signature.write_text(receipt["signature"])
        result = subprocess.run(
            ["ssh-keygen", "-Y", "find-principals", "-f", str(allowed_signers),
             "-s", str(signature)], capture_output=True, check=False,
        )
        if result.returncode:
            raise ValueError("Local reviewer signer is not enrolled")
        for principal in result.stdout.decode().splitlines():
            verified = subprocess.run(
                ["ssh-keygen", "-Y", "verify", "-f", str(allowed_signers), "-I", principal,
                 "-n", NAMESPACE, "-s", str(signature)], input=canonical(receipt["payload"]),
                capture_output=True, check=False,
            )
            if verified.returncode == 0:
                return
    raise ValueError("Invalid local review signature")


def digest(receipt: dict[str, Any]) -> str:
    return hashlib.sha256(canonical(receipt)).hexdigest()
