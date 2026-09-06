"""Result cell display and clipboard representations."""

from __future__ import annotations

from decimal import Decimal
from numbers import Integral, Real

import pandas as pd
from rich.text import Text

_MAX_CELL_LENGTH = 512


def _format_cell(value: object, *, clipboard: bool = False) -> str:
    if value is None:
        return "NULL"
    try:
        if bool(pd.isna(value)):
            return "NULL"
    except (TypeError, ValueError):
        pass
    if clipboard:
        rendered = str(value)
    elif isinstance(value, Decimal) and value.is_finite():
        normalized = Decimal(0) if value == 0 else value.normalize()
        rendered = format(normalized, ",f")
    elif isinstance(value, Integral) and not isinstance(value, bool):
        rendered = format(value, ",d")
    elif isinstance(value, Real) and not isinstance(value, bool):
        rendered = format(value, ",")
    else:
        rendered = str(value)
    rendered = (
        rendered.replace("\r\n", "\\n")
        .replace("\r", "\\n")
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )
    if not clipboard and len(rendered) > _MAX_CELL_LENGTH:
        return rendered[: _MAX_CELL_LENGTH - 1] + "…"
    return rendered


class ResultCell(Text):
    def __init__(self, value: object) -> None:
        super().__init__(_format_cell(value))
        self.clipboard_text = _format_cell(value, clipboard=True)


def copy_cell(value: object) -> str:
    return value.clipboard_text if isinstance(value, ResultCell) else str(value)
