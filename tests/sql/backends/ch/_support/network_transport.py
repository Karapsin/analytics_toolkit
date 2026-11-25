from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace
from typing import Any

from tests.sql.backends.ch._support.exact_transport import native_context


def encode_normalized(
    spec: str,
    values: list[Any],
    transport: str,
    *,
    settings: dict[str, Any] | None = None,
) -> bytes:
    """Encode emitted adapter values once with the installed driver's real codec."""
    if transport == "native":
        from clickhouse_driver.bufferedwriter import BufferedSocketWriter  # noqa: PLC0415
        from clickhouse_driver.columns.service import write_column  # noqa: PLC0415

        output = BytesIO()
        writer = BufferedSocketWriter(SimpleNamespace(sendall=output.write), 256)
        context = native_context()
        context.settings.update(settings or {})
        write_column(context, "address", spec, list(values), writer)
        writer.flush()
        return output.getvalue()

    from clickhouse_connect.datatypes.registry import get_from_name  # noqa: PLC0415
    from clickhouse_connect.driver.insert import InsertContext  # noqa: PLC0415

    datatype = get_from_name(spec)
    context = InsertContext("db.target", ["address"], [datatype], settings=settings)
    context.start_column("address")
    output_bytes = bytearray()
    datatype.write_column(list(values), output_bytes, context)
    return bytes(output_bytes)
