"""Choose an installed Linux monospace family for headless SVG rendering."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TypedDict


class RenderOptions(TypedDict, total=False):
    """Supported optional renderer arguments."""

    monospace_family: str
    font_files: list[str]


def render_options() -> RenderOptions:
    """Preserve platform defaults unless the known Debian font is available."""
    font = Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf")
    if sys.platform.startswith("linux") and font.is_file():
        # resvg-py 0.5 selects Liberation Mono for generic monospace on Linux.
        # Bookworm's Python image contains DejaVu instead; name and load it.
        return {"monospace_family": "DejaVu Sans Mono", "font_files": [str(font)]}
    return {}
