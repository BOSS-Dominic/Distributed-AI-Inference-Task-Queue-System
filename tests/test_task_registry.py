"""Contract tests for the shared task registry (single source of truth)."""

from shared.task_registry import (
    DEFAULT_PRIORITY,
    PRIORITY_LEVELS,
    TASK_REGISTRY,
    get_expected_time,
    get_heavy_types,
    get_light_types,
    get_priority_score,
    get_queue_name,
)

EXPECTED_TIMES = {"IC": 0.3, "SA": 0.8, "SIM": 3.4, "SUM": 11.6}


def test_four_task_types():
    assert set(TASK_REGISTRY) == {"IC", "SA", "SIM", "SUM"}


def test_expected_times_calibrated():
    assert {k: v["expected_time"] for k, v in TASK_REGISTRY.items()} == EXPECTED_TIMES


def test_queue_assignment():
    assert TASK_REGISTRY["IC"]["queue"] == "heavy"
    for t in ("SA", "SIM", "SUM"):
        assert TASK_REGISTRY[t]["queue"] == "light"


def test_io_flags():
    assert TASK_REGISTRY["IC"]["requires_gpu"] is True
    assert all(not TASK_REGISTRY[t]["requires_gpu"] for t in ("SA", "SIM", "SUM"))
    assert TASK_REGISTRY["IC"]["requires_file"] is True
    assert TASK_REGISTRY["SIM"]["requires_text_pair"] is True
    assert not any(TASK_REGISTRY[t]["requires_text_pair"] for t in ("IC", "SA", "SUM"))


def test_priority_levels():
    assert PRIORITY_LEVELS == {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    assert DEFAULT_PRIORITY == "MEDIUM"
    assert get_priority_score("HIGH") == 0
    assert get_priority_score("LOW") == 2
    assert get_priority_score("UNKNOWN") == 1


def test_queue_helpers():
    assert get_queue_name("IC") == "queue:heavy"
    assert get_heavy_types() == ["IC"]
    assert set(get_light_types()) == {"SA", "SIM", "SUM"}
    assert get_expected_time("SUM") == 11.6
