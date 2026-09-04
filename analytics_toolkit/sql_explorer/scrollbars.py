"""Scrollbar layout helpers shared by SQL Explorer workspace widgets."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

    from textual.geometry import Region
    from textual.widget import Widget


class LeftVerticalScrollbarMixin:
    """Place Textual's vertical scrollbar on the left of the content region."""

    def _get_scrollable_region(self: Any, region: Region) -> Region:
        show_vertical, show_horizontal = self.scrollbars_enabled
        vertical_size = self.styles.scrollbar_size_vertical
        horizontal_size = self.styles.scrollbar_size_horizontal
        show_vertical = bool(show_vertical and vertical_size)
        show_horizontal = bool(show_horizontal and horizontal_size)
        if self.styles.scrollbar_gutter == "stable":
            show_vertical = True

        if show_vertical:
            _, region = region.split_vertical(vertical_size)
        if show_horizontal:
            region, _ = region.split_horizontal(-horizontal_size)
        return region

    def _arrange_scrollbars(
        self: Any,
        region: Region,
    ) -> Iterable[tuple[Widget, Region]]:
        show_vertical, show_horizontal = self.scrollbars_enabled
        vertical_size = self.scrollbar_size_vertical
        horizontal_size = self.scrollbar_size_horizontal
        show_vertical = bool(show_vertical and vertical_size)
        show_horizontal = bool(show_horizontal and horizontal_size)

        if show_vertical and show_horizontal:
            vertical_column, content_column = region.split_vertical(vertical_size)
            vertical_region, corner_region = vertical_column.split_horizontal(-horizontal_size)
            window_region, horizontal_region = content_column.split_horizontal(-horizontal_size)
            yield self.scrollbar_corner, corner_region
            if vertical_region:
                vertical = self.vertical_scrollbar
                vertical.window_virtual_size = self.virtual_size.height
                vertical.window_size = window_region.height
                yield vertical, vertical_region
            if horizontal_region:
                horizontal = self.horizontal_scrollbar
                horizontal.window_virtual_size = self.virtual_size.width
                horizontal.window_size = window_region.width
                yield horizontal, horizontal_region
            return

        if show_vertical:
            vertical_region, window_region = region.split_vertical(vertical_size)
            if vertical_region:
                vertical = self.vertical_scrollbar
                vertical.window_virtual_size = self.virtual_size.height
                vertical.window_size = window_region.height
                yield vertical, vertical_region
            return

        if show_horizontal:
            window_region, horizontal_region = region.split_horizontal(-horizontal_size)
            if horizontal_region:
                horizontal = self.horizontal_scrollbar
                horizontal.window_virtual_size = self.virtual_size.width
                horizontal.window_size = window_region.width
                yield horizontal, horizontal_region


__all__ = ["LeftVerticalScrollbarMixin"]
