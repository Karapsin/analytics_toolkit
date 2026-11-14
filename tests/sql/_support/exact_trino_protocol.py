"""Byte-level Trino HTTP pages with real SDK request and cursor machinery."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import requests

if TYPE_CHECKING:
    import pytest

_STATEMENT = "http://trino.example:8080/v1/statement"
_QUERY = _STATEMENT + "/exact_protocol/"


def page(
    columns: list[dict[str, Any]],
    raw_data: bytes,
    *,
    next_page: int | None = None,
    update_count: int | None = None,
) -> bytes:
    """Keep raw object pairs intact; serializing a dict would hide duplicates."""
    body: dict[str, Any] = {
        "id": "exact_protocol",
        "infoUri": "http://trino.example/query/exact_protocol",
        "stats": {"state": "FINISHED" if next_page is None else "RUNNING"},
        "columns": columns,
    }
    if next_page is not None:
        body["nextUri"] = _QUERY + str(next_page)
    if update_count is not None:
        body.update(updateType="INSERT", updateCount=update_count)
    return json.dumps(body).encode("utf-8")[:-1] + b',"data":' + raw_data + b"}"


@dataclass
class Protocol:
    methods: list[str] = field(default_factory=list)
    statements: list[str] = field(default_factory=list)
    timeouts: list[Any] = field(default_factory=list)
    headers: list[dict[str, Any]] = field(default_factory=list)
    closed_sessions: list[requests.Session] = field(default_factory=list)


def install_protocol(
    monkeypatch: pytest.MonkeyPatch,
    pages: list[bytes],
    *,
    request_error: requests.exceptions.RequestException | None = None,
    delete_status: int = 204,
    response_status: int = 200,
) -> Protocol:
    """Intercept only transport I/O and observe genuine session disposal."""
    protocol = Protocol()
    close = requests.Session.close

    def request(_session: Any, method: str, url: str, **kwargs: Any) -> requests.Response:
        method = method.upper()
        protocol.methods.append(method)
        protocol.timeouts.append(kwargs.get("timeout"))
        protocol.headers.append(dict(kwargs.get("headers", {})))
        if request_error is not None:
            raise request_error
        response = requests.Response()
        if method == "POST":
            assert url == _STATEMENT
            protocol.statements.append(kwargs["data"].decode("utf-8"))
            content = pages[0]
        elif method == "GET":
            assert url.startswith(_QUERY)
            content = pages[int(url.removeprefix(_QUERY))]
        else:
            assert method == "DELETE"
            assert url.startswith(_QUERY)
            response.status_code = delete_status
            response._content = b"private-cancellation-error" if delete_status != 204 else b""
            return response
        response.status_code = response_status
        response._content = content
        response.headers["Content-Type"] = "application/json"
        return response

    def close_session(session: requests.Session) -> None:
        protocol.closed_sessions.append(session)
        close(session)

    monkeypatch.setattr(requests.Session, "request", request)
    monkeypatch.setattr(requests.Session, "close", close_session)
    return protocol
