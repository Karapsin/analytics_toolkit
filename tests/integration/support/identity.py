from __future__ import annotations

import hashlib
import re


def safe_identifier(value: str, *, limit: int = 63) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_") or "scenario"
    if len(normalized.encode("utf-8")) <= limit:
        return normalized
    digest = hashlib.sha256(normalized.encode()).hexdigest()[:10]
    prefix = normalized[: max(1, limit - len(digest) - 1)]
    return f"{prefix}_{digest}"


def resource_name(run_id: str, test_id: str, purpose: str, *, limit: int = 63) -> str:
    return safe_identifier(f"it_{run_id}_{test_id}_{purpose}", limit=limit)


def query_label(run_id: str, test_id: str, purpose: str) -> str:
    return f"analytics_toolkit_integration:{run_id}:{test_id}:{safe_identifier(purpose)}"
