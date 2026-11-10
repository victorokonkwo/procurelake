"""Exponential backoff with full jitter, honouring server-provided Retry-After."""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 6
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 60.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.base_delay_seconds <= 0 or self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("require 0 < base_delay_seconds <= max_delay_seconds")

    def delay_before_retry(
        self,
        failed_attempt: int,
        retry_after_seconds: float | None = None,
        rng: random.Random | None = None,
    ) -> float:
        """Seconds to wait after ``failed_attempt`` (1-based) before trying again.

        Full jitter: uniform(0, min(max_delay, base * 2**(attempt-1))). A Retry-After value
        from the server takes precedence when it is larger, capped at ``max_delay_seconds``.
        """
        if failed_attempt < 1:
            raise ValueError("failed_attempt is 1-based")
        ceiling = min(self.max_delay_seconds, self.base_delay_seconds * 2 ** (failed_attempt - 1))
        jittered = (rng or random).uniform(0, ceiling)
        if retry_after_seconds is not None and retry_after_seconds > jittered:
            return min(retry_after_seconds, self.max_delay_seconds)
        return jittered
