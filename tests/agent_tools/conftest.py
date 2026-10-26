"""Keep renderer-free agent tool tests collectable on every supported interpreter."""

import sys

# resvg-py>=0.5 publishes no Python 3.9 builds, so the actual-renderer regressions and
# the diagnostics that import them run only where the pinned renderer can install.
collect_ignore = []
if sys.version_info < (3, 10):
    collect_ignore = ["underline_rasterization.py", "raster_observations.py"]
