"""A manually controlled clock for deterministic timeout and progress tests."""

from __future__ import annotations


class FakeClock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    @property
    def now(self) -> float:
        return self.value

    def __call__(self) -> float:
        return self.value

    def monotonic(self) -> float:
        return self.value

    def set(self, value: float) -> None:
        self.value = value

    def sleep(self, seconds: float) -> None:
        self.value += seconds
