from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Timeline:
    parties: int = 2
    timeout: float = 15
    barrier: threading.Barrier = field(init=False)
    intervals: dict[str, tuple[float, float]] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.barrier = threading.Barrier(self.parties)

    def step(self, name: str, value: Any) -> Any:
        entered = time.monotonic()
        self.events.append({"task": name, "phase": "entered", "at": entered})
        self.barrier.wait(timeout=self.timeout)
        result = value() if callable(value) else value
        exited = time.monotonic()
        self.events.append({"task": name, "phase": "exited", "at": exited})
        self.intervals[name] = (entered, exited)
        return result

    def assert_overlap(self, first: str, second: str) -> None:
        first_start, first_end = self.intervals[first]
        second_start, second_end = self.intervals[second]
        assert max(first_start, second_start) < min(first_end, second_end)
