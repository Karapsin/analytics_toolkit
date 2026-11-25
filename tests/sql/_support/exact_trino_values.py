"""Raw HTTP fixtures retaining the actual Trino SDK request and decoder path."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import requests

if TYPE_CHECKING:
    from collections.abc import Sequence

    import pytest


def raw_type(raw: str, *arguments: dict[str, Any]) -> dict[str, Any]:
    return {"rawType": raw, "arguments": list(arguments)}


def timestamp_type(precision: int = 9, *, aware: bool = False) -> dict[str, Any]:
    raw = "timestamp with time zone" if aware else "timestamp"
    return raw_type(raw, {"kind": "LONG", "value": precision})


def column(name: str, native: str, signature: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "type": native, "typeSignature": signature}


def raw_response(
    monkeypatch: pytest.MonkeyPatch,
    columns: list[dict[str, Any]],
    rows: list[list[Any]],
    *,
    next_rows: list[list[Any]] | None = None,
    calls: list[str] | None = None,
) -> list[str]:
    """Replace HTTP only: real connection, SDK request and row mapper remain active."""
    pages = [rows] if next_rows is None else [rows, next_rows]
    return raw_pages_response(monkeypatch, columns, pages, calls=calls)


def raw_pages_response(
    monkeypatch: pytest.MonkeyPatch,
    columns: list[dict[str, Any]],
    pages: Sequence[list[list[Any]]],
    *,
    calls: list[str] | None = None,
) -> list[str]:
    """Serve any number of raw result pages through the installed real SDK."""
    if not pages:
        message = "At least one raw Trino response page is required."
        raise ValueError(message)
    page_uri = "http://trino.example:8080/v1/statement/exact_read_query/"
    page_indexes = {page_uri + str(index): index for index in range(1, len(pages))}

    def body(index: int) -> bytes:
        response: dict[str, Any] = {
            "id": "exact_read_query",
            "infoUri": "http://trino.example/query/exact_read_query",
            "stats": {"state": "FINISHED"},
            "columns": columns,
            "data": pages[index],
        }
        if index + 1 < len(pages):
            response["nextUri"] = page_uri + str(index + 1)
        return json.dumps(response).encode("utf-8")

    statements: list[str] = []

    def request(_session: Any, method: str, url: str, **kwargs: Any) -> requests.Response:
        method = method.upper()
        if calls is not None:
            calls.append(method)
        if method == "POST":
            assert url == "http://trino.example:8080/v1/statement"
            statements.append(kwargs["data"].decode("utf-8"))
            content = body(0)
        else:
            assert method == "GET"
            assert url in page_indexes
            content = body(page_indexes[url])
        response = requests.Response()
        response.status_code = 200
        response._content = content
        response.headers["Content-Type"] = "application/json"
        return response

    monkeypatch.setattr(requests.Session, "request", request)
    return statements
