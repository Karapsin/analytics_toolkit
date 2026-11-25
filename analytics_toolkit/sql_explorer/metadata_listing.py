"""Names-only metadata results retain completeness without changing tuple callers."""

from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING, Any, Tuple

if TYPE_CHECKING:
    from collections.abc import Iterable


class MetadataListing(Tuple[str, ...]):  # noqa: SLOT001 -- tuple coverage attributes require __dict__.
    """Exact names plus operation coverage, compatible with historical tuples."""

    complete: bool
    cluster_status: tuple[dict[str, Any], ...]

    def __new__(  # noqa: PYI034 -- preserve the Python 3.8 annotation floor without runtime extras.
        cls,
        names: Iterable[str] = (),
        *,
        complete: bool = True,
        cluster_status: Iterable[dict[str, Any]] = (),
    ) -> MetadataListing:
        result = super().__new__(cls, names)
        result.complete = complete
        result.cluster_status = tuple(cluster_status)
        return result


class IncompleteMetadataError(RuntimeError):
    """The last complete snapshot remains authoritative during an outage."""


def listing_failure(exc: Exception) -> Exception:
    """Identify recoverable listing notices without exposing their scope or values."""
    with suppress(AttributeError, TypeError):
        exc.analytics_toolkit_metadata_listing_failure = True  # type: ignore[attr-defined]
    return exc


def listing_result(values: Iterable[str]) -> MetadataListing:
    return MetadataListing(
        values,
        complete=getattr(values, "complete", True),
        cluster_status=getattr(values, "cluster_status", ()),
    )
