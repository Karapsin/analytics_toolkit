from __future__ import annotations

import json
import os
import resource
import sys
import threading
import time
from pathlib import Path


def current_rss_bytes() -> int:
    statm = Path("/proc/self/statm")
    if statm.exists():
        resident_pages = int(statm.read_text(encoding="utf-8").split()[1])
        return resident_pages * int(os.sysconf("SC_PAGE_SIZE"))
    maximum = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(maximum if sys.platform == "darwin" else maximum * 1024)


class MemorySampler:
    def __init__(self, artifact: Path, *, interval: float = 0.05) -> None:
        self.artifact = artifact
        self.interval = interval
        self.samples: list[dict[str, float | int]] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._sample, name="integration-rss-sampler")

    def __enter__(self) -> MemorySampler:  # noqa: PYI034 - Python 3.8 cannot import Self.
        self._thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self._stop.set()
        self._thread.join(5)
        self.artifact.parent.mkdir(parents=True, exist_ok=True)
        self.artifact.write_text(json.dumps(self.samples, indent=2), encoding="utf-8")

    @property
    def growth_bytes(self) -> int:
        values = [int(sample["rss_bytes"]) for sample in self.samples]
        return max(values) - values[0] if values else 0

    def _sample(self) -> None:
        while not self._stop.wait(self.interval):
            self.samples.append({"monotonic": time.monotonic(), "rss_bytes": current_rss_bytes()})
