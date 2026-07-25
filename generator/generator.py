"""
Workload generator — injects tasks using a Poisson arrival process. v1.5
Invoked by Dashboard via:
  docker exec workload-generator python generator.py <total> <rate> <ic> <sa> <sim> <sum> [priority mix]
"""

import logging
import os
import random
import sys
import time

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("generator")

from shared.jailbreak_words import JAILBREAK_PAYLOADS
from shared.task_registry import TASK_REGISTRY

PRIORITY_KEYS = ["HIGH", "MEDIUM", "LOW"]
DEFAULT_PRIORITY_MIX = (2, 5, 3)  # HIGH:MEDIUM:LOW ratio

API_GATEWAY = os.getenv("API_GATEWAY_URL", "http://fastapi-gateway:8000")
SEED_IMAGES_DIR = os.getenv("SEED_IMAGES_DIR", "/app/storage/inputs/seeds")
PROMPTS_FILE = os.getenv("PROMPTS_FILE", "/app/storage/inputs/prompts.txt")

_seed_images: list[str] = []
_seed_prompts: list[str] = []


def _load_seeds():
    global _seed_images, _seed_prompts

    if os.path.isdir(SEED_IMAGES_DIR):
        _seed_images = [
            os.path.join(SEED_IMAGES_DIR, f)
            for f in os.listdir(SEED_IMAGES_DIR)
            if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.bmp'))
        ]
        logger.info("Loaded %d seed images from %s.", len(_seed_images), SEED_IMAGES_DIR)
    else:
        logger.warning("Seed images directory not found: %s", SEED_IMAGES_DIR)

    if os.path.isfile(PROMPTS_FILE):
        with open(PROMPTS_FILE, "r", encoding="utf-8") as f:
            _seed_prompts = [line.strip() for line in f if line.strip()]
        logger.info("Loaded %d prompts from %s.", len(_seed_prompts), PROMPTS_FILE)
    else:
        logger.warning("Prompts file not found: %s - using defaults.", PROMPTS_FILE)
        _seed_prompts = [
            "The product arrived on time and works perfectly.",
            "I am extremely disappointed with the quality.",
            "The customer service was very helpful.",
            "This is the worst experience I have ever had.",
            "Neutral statement about the weather today.",
            "The movie was okay, nothing special.",
            "I love this brand, they never let me down.",
            "The software keeps crashing when I save.",
            "Fairly priced for the features, would recommend.",
            "Absolutely terrible, do not buy this.",
        ]


def inject_workload(total_tasks: int = 30, arrival_rate: float = 20.0,
                    ic_count: int | None = None, sa_count: int | None = None,
                    sim_count: int | None = None, sum_count: int | None = None):
    if not _seed_images and not _seed_prompts:
        _load_seeds()

    # defaults: split evenly
    if ic_count is None:
        ic_count = total_tasks // 4
    if sa_count is None:
        sa_count = total_tasks // 4
    if sim_count is None:
        sim_count = total_tasks // 4
    if sum_count is None:
        sum_count = total_tasks - ic_count - sa_count - sim_count

    logger.info("=" * 60)
    logger.info("Poisson Workload Injection")
    logger.info("Total: %d, Rate: %s/s, IC: %d, SA: %d, SIM: %d, SUM: %d",
                total_tasks, arrival_rate, ic_count, sa_count, sim_count, sum_count)
    logger.info("=" * 60)

    task_sequence = (["IC"] * ic_count) + (["SA"] * sa_count) + (["SIM"] * sim_count) + (["SUM"] * sum_count)
    random.shuffle(task_sequence)

    submitted = 0
    ic_submitted = sa_submitted = sim_submitted = sum_submitted = 0
    jailbreak_count = 0
    fail_count = 0
    sa_counter = sim_counter = sum_counter = 0

    t0 = time.time()

    for i, task_type in enumerate(task_sequence):
        R = TASK_REGISTRY[task_type]

        try:
            if R["requires_file"] and _seed_images:
                img_path = random.choice(_seed_images)
                with open(img_path, "rb") as f:
                    file_content = f.read()
                files = {
                    "file": (os.path.basename(img_path), file_content, "image/jpeg"),
                }
                resp = requests.post(
                    f"{API_GATEWAY}/v1/inference/tasks",
                    data={"task_type": task_type},
                    files=files,
                    timeout=10,
                )
                if resp.status_code == 202:
                    submitted += 1
                    ic_submitted += 1
                else:
                    fail_count += 1

            elif R.get("requires_text_pair"):
                sim_counter += 1
                if sim_counter > 0 and sim_counter % 10 == 0:
                    text1 = random.choice(JAILBREAK_PAYLOADS)
                    jailbreak_count += 1
                    text2 = random.choice(_seed_prompts)
                else:
                    text1 = random.choice(_seed_prompts)
                    text2 = random.choice(_seed_prompts)
                prio = random.choices(PRIORITY_KEYS, weights=DEFAULT_PRIORITY_MIX, k=1)[0]
                resp = requests.post(
                    f"{API_GATEWAY}/v1/inference/tasks",
                    data={"task_type": task_type, "text_data": text1, "text_data_2": text2, "priority": prio},
                    timeout=10,
                )
                if resp.status_code == 202:
                    submitted += 1
                    sim_submitted += 1
                else:
                    fail_count += 1

            else:
                # SA or SUM — single text
                is_sa = (task_type == "SA")
                if is_sa:
                    sa_counter += 1
                else:
                    sum_counter += 1
                counter = sa_counter if is_sa else sum_counter
                if counter > 0 and counter % 10 == 0:
                    text = random.choice(JAILBREAK_PAYLOADS)
                    jailbreak_count += 1
                else:
                    text = random.choice(_seed_prompts)

                prio = random.choices(PRIORITY_KEYS, weights=DEFAULT_PRIORITY_MIX, k=1)[0]
                resp = requests.post(
                    f"{API_GATEWAY}/v1/inference/tasks",
                    data={"task_type": task_type, "text_data": text, "priority": prio},
                    timeout=10,
                )
                if resp.status_code == 202:
                    submitted += 1
                    if is_sa:
                        sa_submitted += 1
                    else:
                        sum_submitted += 1
                else:
                    fail_count += 1

        except requests.exceptions.RequestException as e:
            fail_count += 1
            logger.warning("Request failed: %s", e)

        if i < len(task_sequence) - 1:
            interval = random.expovariate(arrival_rate)
            time.sleep(interval)

    elapsed = time.time() - t0

    logger.info("=" * 60)
    logger.info("Workload injection complete.")
    logger.info("  Elapsed:        %.2fs", elapsed)
    logger.info("  Submitted:      %d/%d", submitted, total_tasks)
    logger.info("  IC: %d, SA: %d, SIM: %d, SUM: %d, Jailbreak: %d, Failures: %d",
                ic_submitted, sa_submitted, sim_submitted, sum_submitted, jailbreak_count, fail_count)
    logger.info("  Effective rate: %.2f tasks/sec", submitted / elapsed if elapsed > 0 else 0)
    logger.info("=" * 60)


if __name__ == "__main__":
    _load_seeds()

    total = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    rate = float(sys.argv[2]) if len(sys.argv) > 2 else 20.0
    ic   = int(sys.argv[3]) if len(sys.argv) > 3 else None
    sa   = int(sys.argv[4]) if len(sys.argv) > 4 else None
    sim  = int(sys.argv[5]) if len(sys.argv) > 5 else None
    sum_ = int(sys.argv[6]) if len(sys.argv) > 6 else None

    inject_workload(total, rate, ic, sa, sim, sum_)
