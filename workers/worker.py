"""
Worker node — heterogeneous GPU/CPU inference with Redis task queue. v1.0
"""

import json
import logging
import os
import signal
import sqlite3
import sys
import time
import traceback

import redis
import requests
import torch
from PIL import Image
from transformers import pipeline

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

REDIS_HOST = os.getenv("REDIS_HOST", "redis-broker")
WORKER_TYPE = os.getenv("WORKER_TYPE", "SLOW")
WORKER_NAME = os.getenv("WORKER_NAME",
    "worker-fast-gpu" if WORKER_TYPE == "FAST" else f"worker-slow-cpu-{os.getpid()}")
API_GATEWAY = os.getenv("API_GATEWAY_URL", "http://fastapi-gateway:8000")
GPU_SLOWDOWN_MS = float(os.getenv("GPU_SLOWDOWN_MS", "0"))
ENABLE_STEALING = os.getenv("ENABLE_WORK_STEALING", "1") == "1"

JAILBREAK_KEYWORDS = [
    "ignore all previous", "do anything now", "dan ",
    "system override", "disable safety", "bypass content",
    "developer mode", "unethical ai", "maintenance mode",
    "red-teaming", "filters disabled", "without restrictions",
    "without any safeguards", "pretend you are",
]

if WORKER_TYPE == "FAST" and "FAST" in WORKER_NAME and WORKER_NAME.startswith("Worker_"):
    WORKER_NAME = "worker-fast-gpu"

r = redis.Redis(
    host=REDIS_HOST, port=6379, db=0,
    decode_responses=True,
    socket_connect_timeout=5,
    socket_keepalive=True,
)


# --- Device selection ---

if WORKER_TYPE == "FAST" and torch.cuda.is_available():
    DEVICE_ID = 0
    DEVICE_NAME = torch.cuda.get_device_name(0)
    logger.info("%s -> %s", WORKER_NAME, DEVICE_NAME)
else:
    DEVICE_ID = -1
    if WORKER_TYPE == "FAST":
        logger.warning("CUDA NOT AVAILABLE - falling back to CPU")
    else:
        logger.info("%s - constrained CPU-only mode", WORKER_NAME)


# --- Model loading ---

logger.info("%s Loading models...", WORKER_NAME)

nlp_engine = pipeline(
    'sentiment-analysis',
    model='distilbert-base-uncased-finetuned-sst-2-english',
    device=DEVICE_ID,
)

cv_engine = None
if WORKER_TYPE == "FAST":
    try:
        cv_engine = pipeline(
            'image-classification',
            model='microsoft/resnet-18',
            device=DEVICE_ID,
        )
        logger.info("%s CV engine (ResNet-18) loaded on device=%s.", WORKER_NAME, DEVICE_ID)
    except Exception as e:
        logger.error("%s CV engine load failed: %s", WORKER_NAME, e)

logger.info("%s Models ready. Entering main loop.", WORKER_NAME)


# Audit write (HTTP callback -> SQLite fallback)

def write_audit(audit_payload: dict):
    # Primary: HTTP callback to FastAPI
    try:
        resp = requests.post(
            f"{API_GATEWAY}/v1/internal/audit",
            json=audit_payload, timeout=5,
        )
        if resp.status_code == 200:
            return
        logger.warning("%s Audit callback returned %s: %s", WORKER_NAME, resp.status_code, resp.text)
    except requests.exceptions.RequestException as exc:
        logger.warning("%s Audit callback unreachable: %s", WORKER_NAME, exc)

    # Fallback: direct SQLite write
    try:
        conn = sqlite3.connect("/app/storage/metrics.db")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute(
            """INSERT OR REPLACE INTO task_history
               (task_id, task_type, worker_name, execution_latency,
                final_status, model_output, input_summary)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                audit_payload["task_id"], audit_payload["task_type"],
                audit_payload["worker_name"], audit_payload["execution_latency"],
                audit_payload["final_status"], audit_payload["model_output"],
                audit_payload.get("input_summary", ""),
            ),
        )
        conn.commit()
        conn.close()
    except Exception as db_err:
        logger.error("%s SQLite fallback failed: %s", WORKER_NAME, db_err)


# --- Graceful shutdown ---

def shutdown(signum, frame):
    logger.info("%s Received signal %s, shutting down.", WORKER_NAME, signum)
    sys.exit(0)

signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)


# --- Main loop ---

heartbeat_counter = 0
logger.info("%s Main loop started.", WORKER_NAME)

while True:
    task_data = None
    stolen = False

    if WORKER_TYPE == "FAST":
        task_data = r.blpop("queue:heavy", timeout=1)
        if task_data is None:
            stealing_enabled = r.get("config:work_stealing") != "0"
            if stealing_enabled and ENABLE_STEALING:
                task_data = r.blpop("queue:light", timeout=1)
                if task_data:
                    stolen = True
    else:
        task_data = r.blpop("queue:light", timeout=1)

    if task_data is None:
        heartbeat_counter += 1
        if heartbeat_counter % 5 == 0:
            r.set(f"worker:{WORKER_NAME}:heartbeat", "alive", ex=15)
        continue

    queue_name, payload_str = task_data
    try:
        payload = json.loads(payload_str)
    except json.JSONDecodeError:
        logger.warning("%s Invalid JSON payload, skipping.", WORKER_NAME)
        continue

    task_id = payload["task_id"]
    task_type = payload["task_type"]
    stolen_tag = " (stolen)" if stolen else ""
    logger.info("%s Processing %s task %s%s", WORKER_NAME, task_type, task_id, stolen_tag)

    r.hset("active:tasks", task_id, "PROCESSING")
    start_time = time.time()
    model_output, final_status = "", "SUCCESS"

    try:
        if task_type == "CV":
            if cv_engine is None:
                raise RuntimeError("CV engine not available")
            img_path = payload.get("image_path", "")
            if not os.path.exists(img_path):
                raise FileNotFoundError(f"Image not found: {img_path}")
            pil_img = Image.open(img_path).convert("RGB")
            results = cv_engine(pil_img)
            model_output = f"{results[0]['label']} ({results[0]['score']:.4f})"

        elif task_type == "NLP":
            text = payload.get("text", "Default query.")
            text_lower = text.lower()
            is_jailbreak = any(kw in text_lower for kw in JAILBREAK_KEYWORDS)
            if is_jailbreak:
                model_output = "JailbreakBlocked: prompt injection detected, request denied"
                final_status = "FAILED"
            else:
                results = nlp_engine(text)
                model_output = f"{results[0]['label']} (score={results[0]['score']:.4f})"
        else:
            model_output = f"Unknown task_type: {task_type}"
            final_status = "FAILED"

    except Exception as e:
        final_status = "FAILED"
        model_output = f"{type(e).__name__}: {str(e)}"
        logger.error("%s Task %s failed: %s", WORKER_NAME, task_id, model_output)
        traceback.print_exc()

    latency = time.time() - start_time

    if WORKER_TYPE == "FAST" and GPU_SLOWDOWN_MS > 0:
        time.sleep(GPU_SLOWDOWN_MS / 1000.0)
        latency = time.time() - start_time

    r.hdel("active:tasks", task_id)

    audit_payload = {
        "task_id": task_id, "task_type": task_type,
        "worker_name": WORKER_NAME,
        "execution_latency": round(latency, 6),
        "final_status": final_status, "model_output": model_output,
        "input_summary": payload.get("input_summary", ""),
    }
    write_audit(audit_payload)
    logger.info("%s Task %s -> %s (%.4fs)", WORKER_NAME, task_id, final_status, latency)

    heartbeat_counter += 1
    r.set(f"worker:{WORKER_NAME}:heartbeat", "alive", ex=15)
