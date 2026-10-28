"""Retry delay contract (shared/retry_logic.py)."""

from shared.retry_logic import retry_delay


def test_immediate_is_zero():
    assert retry_delay("immediate", 1, 2.0) == 0.0


def test_backoff_doubles_each_attempt():
    assert retry_delay("backoff", 1, 2.0) == 2.0
    assert retry_delay("backoff", 2, 2.0) == 4.0
    assert retry_delay("backoff", 3, 2.0) == 8.0


def test_backoff_jitter_within_bounds():
    for attempt in (1, 2, 3):
        base = 2.0 * 2 ** (attempt - 1)
        for _ in range(50):
            delay = retry_delay("backoff_jitter", attempt, 2.0)
            assert base <= delay < base + 1.0


def test_unknown_mode_falls_back_to_jitter():
    delay = retry_delay("something_else", 2, 2.0)
    assert 4.0 <= delay < 5.0
