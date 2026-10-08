"""Construct the SDK client and report API request IDs without logging credentials."""

from __future__ import annotations

import math
import time
from contextlib import contextmanager
from threading import Lock
from typing import Any, Iterator

from datalens_sdk import DataLensClientYC, StaticYCIAMAuthProvider

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
    organization = state.runtime["organization_id"]

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
    auth = StaticYCIAMAuthProvider(
        org_id=state.runtime["organization_id"], token=extract_credentials()
    )
    with DataLensClientYC(auth=auth, event_hooks={"request": [pace_request]}) as client:
        yield client
