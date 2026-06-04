from __future__ import annotations

from typing import NoReturn


def raise_unsupported_module_import(module_name: str) -> NoReturn:
    raise ImportError(
        f"{module_name} is an internal analytics_toolkit.sql module path and "
        "is not a supported import target. Import SQL as "
        "`from analytics_toolkit import sql` or "
        "`import analytics_toolkit.sql as sql`, then call the public SQL facade."
    )
