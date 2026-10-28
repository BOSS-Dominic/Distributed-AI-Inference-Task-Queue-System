"""Retry delay math — pure logic, no I/O.

Shared by the worker (runtime) and the test suite (CI). Keeping the formula
here makes it unit-testable without importing torch/transformers.
"""

import random


def retry_delay(mode: str, attempt: int, base: float) -> float:
    """Compute the delay (seconds) before the next attempt.

    attempt is 1-based: attempt 1 is the first retry after the initial failure.
    Modes: "immediate" (0 s), "backoff" (base * 2^(n-1)),
    "backoff_jitter" (backoff + uniform(0, 1) noise).
    """
    if mode == "immediate":
        return 0.0
    exp = base * (2 ** (attempt - 1))
    if mode == "backoff":
        return exp
    return exp + random.uniform(0, 1)
