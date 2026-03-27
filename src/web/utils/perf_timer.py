"""
Performance Timer - Simple timing utilities for debugging and profiling.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Optional

from src.shared.logging.logger import get_logger

if TYPE_CHECKING:
    from src.shared.logging.logger import PipelineLogger


class PerfTimer:
    """Simple performance timer for logging request durations."""

    def __init__(self, name: str, logger: Optional["PipelineLogger"] = None):
        self.name = name
        self.start = time.perf_counter()
        self.checkpoints: list[tuple[str, float]] = []
        self._logger = logger or get_logger(__name__)

    def checkpoint(self, label: str) -> float:
        """Record a checkpoint and return elapsed time in ms."""
        elapsed = (time.perf_counter() - self.start) * 1000
        self.checkpoints.append((label, elapsed))
        self._logger.info(f"[PERF] {self.name} | {label}: {elapsed:.1f}ms")
        return elapsed

    def done(self) -> float:
        """Log total elapsed time and return it in ms."""
        total = (time.perf_counter() - self.start) * 1000
        self._logger.info(f"[PERF] {self.name} | TOTAL: {total:.1f}ms")
        return total

    @property
    def elapsed_ms(self) -> float:
        """Get current elapsed time in milliseconds."""
        return (time.perf_counter() - self.start) * 1000
