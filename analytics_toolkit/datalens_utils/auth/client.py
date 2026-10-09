"""Construct the SDK client and report API request IDs without logging credentials."""

from __future__ import annotations

import math
import os
import time
from contextlib import contextmanager
from threading import Lock
from typing import Any, Iterator

from datalens_sdk import (
    DataLensClientEnterprise,
    DataLensClientYC,
    OAuthAuthProvider,
    StaticYCIAMAuthProvider,
)

from analytics_toolkit.datalens_utils.errors import DataLensUtilsError
from analytics_toolkit.datalens_utils.session import current_session as session

from .credentials import extract_credentials

_MAX_INTERVAL_SECONDS = 60
_PACING_LOCK = Lock()
_NEXT_REQUEST: dict[str, float] = {}


@contextmanager
def datalens_client() -> Iterator[Any]:
    state = session()
    interval = state.runtime.get("request_interval_seconds", 1.2)
    if (
        not isinstance(interval, (int, float))
        or isinstance(interval, bool)
        or not math.isfinite(interval)
        or interval < 0
        or interval > _MAX_INTERVAL_SECONDS
    ):
        message = "runtime.json request_interval_seconds must be between 0 and 60."
        raise DataLensUtilsError(message)
    installation = state.runtime.get("installation", "yc")
    base_url = getattr(state.deployment, "base_url", None)
    organization = (
        ("yc:" + str(state.runtime["organization_id"]))
        if installation == "yc"
        else "enterprise:" + str(base_url).rstrip("/")
    )

    def pace_request(_request: Any) -> Any:
        # Public SDK/httpx hook: do not inspect or log authenticated requests.
        state.runtime["_request_count"] = state.runtime.get("_request_count", 0) + 1
        with _PACING_LOCK:
            delay = _NEXT_REQUEST.get(organization, 0.0) - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            _NEXT_REQUEST[organization] = time.monotonic() + interval

    if state.client_factory is not None:
        with state.client_factory(state) as client:
            yield client
        return
    token_env = getattr(state.deployment, "token_env", None)
    token = None
    if token_env is not None:
        token = os.environ.get(token_env)
        if not token:
            raise DataLensUtilsError(
                "Missing authentication environment reference: " + token_env + "."
            )
    kwargs: dict[str, Any] = {"event_hooks": {"request": [pace_request]}}
    if installation == "enterprise":
        factory = DataLensClientEnterprise
        kwargs["base_url"] = base_url
        if token is not None:
            kwargs["auth"] = OAuthAuthProvider(token=token)
    else:
        factory = DataLensClientYC
        kwargs["auth"] = StaticYCIAMAuthProvider(
            org_id=state.runtime["organization_id"],
            token=token if token is not None else extract_credentials(),
        )
    with factory(**kwargs) as client:
        yield client
