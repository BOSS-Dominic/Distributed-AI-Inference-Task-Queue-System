"""
Task type registry — single source of truth for all task metadata. v1.5
Imported by api, worker, generator, and dashboard.
"""

# expected_time values are the SETF scheduling scores. They are calibrated from
# the 2026-08-13 pilot run (mean execution_latency per task type, seconds)
TASK_REGISTRY = {
    "IC": {
        "name": "Image Captioning",
        "icon": "",
        "color": "#a371f7",
        "queue": "heavy",
        "requires_gpu": True,
        "requires_file": True,
        "requires_text_pair": False,
        "model_name": "Salesforce/blip-image-captioning-base",
        "package": "transformers",
        "engine_key": "blip",
        "expected_time": 0.3,   # measured ~0.30 s (GPU)
    },
    "SA": {
        "name": "Sentiment Analysis",
        "icon": "",
        "color": "#3fb950",
        "queue": "light",
        "requires_gpu": False,
        "requires_file": False,
        "requires_text_pair": False,
        "model_name": "distilbert-base-uncased-finetuned-sst-2-english",
        "package": "transformers",
        "engine_key": "distilbert",
        "expected_time": 0.8,   # measured ~0.84 s
    },
    "SIM": {
        "name": "Semantic Similarity",
        "icon": "",
        "color": "#2f81f7",
        "queue": "light",
        "requires_gpu": False,
        "requires_file": False,
        "requires_text_pair": True,
        "model_name": "all-MiniLM-L6-v2",
        "package": "sentence_transformers",
        "engine_key": "sbert",
        "expected_time": 3.4,   # measured ~3.4 s under CPU contention
    },
    "SUM": {
        "name": "Text Summarization",
        "icon": "",
        "color": "#d29922",
        "queue": "light",
        "requires_gpu": False,
        "requires_file": False,
        "requires_text_pair": False,
        "model_name": "t5-small",
        "package": "transformers",
        "engine_key": "t5",
        "expected_time": 11.6,  # measured ~11.6 s under CPU contention
    },
}

PRIORITY_LEVELS = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
DEFAULT_PRIORITY = "MEDIUM"


def get_queue_name(task_type: str) -> str:
    return f"queue:{TASK_REGISTRY[task_type]['queue']}"


def get_heavy_types() -> list[str]:
    return [t for t, info in TASK_REGISTRY.items() if info["queue"] == "heavy"]


def get_light_types() -> list[str]:
    return [t for t, info in TASK_REGISTRY.items() if info["queue"] == "light"]


def get_color(task_type: str) -> str:
    return TASK_REGISTRY.get(task_type, {}).get("color", "#808080")


def get_name(task_type: str) -> str:
    return TASK_REGISTRY.get(task_type, {}).get("name", task_type)

def get_expected_time(task_type: str) -> float:
    return TASK_REGISTRY.get(task_type, {}).get("expected_time", 2.0)

def get_priority_score(priority: str) -> int:
    return PRIORITY_LEVELS.get(priority, 1)
