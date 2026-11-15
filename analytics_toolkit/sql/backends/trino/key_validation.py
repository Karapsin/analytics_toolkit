"""Owned views of complete schema-typed Trino effective-key identities."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

    import pandas as pd


def key_validation_frames(
    df: pd.DataFrame,
    key_columns: Sequence[str] | None,
    schemas: Sequence[Mapping[str, str] | None],
    *,
    destination_timezone: str | None = None,
) -> Iterator[pd.DataFrame]:
    """Compare declared native casts without changing retained input values."""
    if not key_columns:
        return
    import pandas as pd  # noqa: PLC0415 -- validation views only.

    from analytics_toolkit.sql.backends.map_values import (  # noqa: PLC0415 -- recursive destination equality.
        _key_identity,
    )
    from analytics_toolkit.sql.backends.type_resolution import (  # noqa: PLC0415 -- native conversion cycle.
        ResolvedColumn,
    )

    seen: set[tuple[tuple[str, str], ...]] = set()
    for schema in schemas:
        types = schema or {}
        columns = tuple(name for name in key_columns if name in types)
        signature = tuple((name, types.get(name, "")) for name in key_columns)
        if not columns or signature in seen:
            continue
        seen.add(signature)
        validation = df.loc[:, list(key_columns)].copy()
        for name in columns:
            conversion = ResolvedColumn(
                types[name], target_backend="trino", destination_timezone=destination_timezone
            )
            values = list(df[name])
            identities = [
                _key_identity(
                    conversion.convert(value),
                    types[name],
                    "trino",
                    destination_timezone,
                    allow_null_components=True,
                )
                for value in values
            ]
            if all(identity is value for identity, value in zip(identities, values)):
                continue
            validation[name] = pd.Series(identities, index=df.index, dtype=object)
        yield validation
