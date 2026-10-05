"""Non-secret identity and defaults attached to successful-query metadata."""

from __future__ import annotations

import hashlib
import json
from dataclasses import fields
from typing import Any

from analytics_toolkit.sql.connection.config import get_connection_config


def metadata_context(connection_key: str) -> dict[str, Any]:
    config = get_connection_config(connection_key)
    values = {field.name: getattr(config, field.name) for field in fields(config)}
    identity = hashlib.sha256(json.dumps(values, sort_keys=True, default=repr).encode()).hexdigest()
    return {
        "identity": identity,
        "catalog": getattr(config, "catalog", None),
        "schema": getattr(config, "schema", None),
        "database": getattr(config, "database", None),
    }


def journal_context(connection_key: str) -> dict[str, Any]:
    """Optional provenance must never replace an execution/configuration error."""
    try:
        return metadata_context(connection_key)
    except Exception:  # noqa: BLE001 -- SQL execution owns connection validation.
        return {}
