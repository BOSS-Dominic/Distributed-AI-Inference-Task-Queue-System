"""
Worker node — heterogeneous GPU/CPU inference with Redis task queue. v1.4
Registry — supports 4 task types across 5 workers.
"""

import json
import logging
import os
import signal
import sqlite3
import sys
import time
import traceback

import numpy as np
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

from shared.jailbreak_words import JAILBREAK_KEYWORDS

REDIS_HOST = os.getenv("REDIS_HOST", "redis-broker")
WORKER_TYPE = os.getenv("WORKER_TYPE", "SLOW")
WORKER_NAME = os.getenv("WORKER_NAME",
    "worker-fast-gpu" if WORKER_TYPE == "FAST" else f"worker-cpu-{os.getpid()}")
API_GATEWAY = os.getenv("API_GATEWAY_URL", "http://fastapi-gateway:8000")
ENABLE_STEALING = os.getenv("ENABLE_WORK_STEALING", "1") == "1"

# legacy compat
if WORKER_TYPE == "FAST" and "FAST" in WORKER_NAME and WORKER_NAME.startswith("Worker_"):
    WORKER_NAME = "worker-fast-gpu"

r = redis.Redis(
    host=REDIS_HOST, port=6379, db=0,
    decode_responses=True,
    socket_connect_timeout=5,
    socket_keepalive=True,
)


# Device selection

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

# determine which task types each worker role can handle
if WORKER_TYPE == "FAST":
    VALID_TYPES = {"IC", "SA", "SIM", "SUM"}
elif WORKER_TYPE == "SIM_SPECIALIST":
    VALID_TYPES = {"SIM"}
elif WORKER_TYPE == "SUM_SPECIALIST":
    VALID_TYPES = {"SUM"}
else:
    VALID_TYPES = {"SA", "SIM", "SUM"}

logger.info("%s role: %s -> handles: %s", WORKER_NAME, WORKER_TYPE, sorted(VALID_TYPES))


# Model loading

logger.info("%s Loading models...", WORKER_NAME)
ENGINES = {}

# BLIP (Image Captioning) — FAST Node only
if "IC" in VALID_TYPES:
    try:
        from transformers import BlipProcessor, BlipForConditionalGeneration
        blip_proc = BlipProcessor.from_pretrained("Salesforce/blip-image-captioning-base")
        blip_model = BlipForConditionalGeneration.from_pretrained(
            "Salesforce/blip-image-captioning-base").to(DEVICE_ID if DEVICE_ID >= 0 else "cpu")
        ENGINES["IC"] = (blip_proc, blip_model)
        logger.info("%s IC engine (BLIP-base) loaded.", WORKER_NAME)
    except Exception as e:
        logger.error("%s IC engine load failed: %s", WORKER_NAME, e)

# SBERT (Semantic Similarity) — FAST, SLOW, SIM_SPECIALIST
if "SIM" in VALID_TYPES:
    try:
        from sentence_transformers import SentenceTransformer
        sim_model = SentenceTransformer("all-MiniLM-L6-v2")
        if DEVICE_ID >= 0 and WORKER_TYPE == "FAST":
            sim_model = sim_model.to("cuda")
        ENGINES["SIM"] = sim_model
        logger.info("%s SIM engine (all-MiniLM-L6-v2) loaded.", WORKER_NAME)
    except Exception as e:
        logger.error("%s SIM engine load failed: %s", WORKER_NAME, e)

# t5 (Text Summarization) — FAST, SLOW, SUM_SPECIALIST
if "SUM" in VALID_TYPES:
    try:
        from transformers import T5Tokenizer, T5ForConditionalGeneration
        t5_tok = T5Tokenizer.from_pretrained("t5-small")
        t5_model = T5ForConditionalGeneration.from_pretrained("t5-small").to(
            DEVICE_ID if DEVICE_ID >= 0 else "cpu")
        ENGINES["SUM"] = (t5_tok, t5_model)
        logger.info("%s SUM engine (t5-small) loaded.", WORKER_NAME)
    except Exception as e:
        logger.error("%s SUM engine load failed: %s", WORKER_NAME, e)

# DistilBERT (Sentiment Analysis) — FAST, SLOW
if "SA" in VALID_TYPES:
    try:
        sa_model = pipeline(
            "sentiment-analysis",
            model="distilbert-base-uncased-finetuned-sst-2-english",
            device=DEVICE_ID,
        )
        ENGINES["SA"] = sa_model
        logger.info("%s SA engine (DistilBERT) loaded.", WORKER_NAME)
    except Exception as e:
        logger.error("%s SA engine load failed: %s", WORKER_NAME, e)

logger.info("%s Models ready (%d engines). Entering main loop.", WORKER_NAME, len(ENGINES))


# Jailbreak check

def _check_jailbreak(text: str) -> bool:
    return any(kw in text.lower() for kw in JAILBREAK_KEYWORDS)


# Audit write (HTTP callback -> SQLite fallback)

def write_audit(audit_payload: dict):
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


# Graceful shutdown

def shutdown(signum, frame):
    logger.info("%s Received signal %s, shutting down.", WORKER_NAME, signum)
    sys.exit(0)

signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)


# Task executors

def execute_ic(payload: dict) -> str:
    """BLIP image captioning."""
    img_path = payload.get("image_path", "")
    if not os.path.exists(img_path):
        raise FileNotFoundError(f"Image not found: {img_path}")
    pil_img = Image.open(img_path).convert("RGB")
    blip_proc, blip_model = ENGINES["IC"]
    inputs = blip_proc(pil_img, return_tensors="pt")
    if DEVICE_ID >= 0:
        inputs = {k: v.to("cuda") for k, v in inputs.items()}
    out = blip_model.generate(**inputs, max_new_tokens=30)
    caption = blip_proc.decode(out[0], skip_special_tokens=True)
    return json.dumps({"caption": caption})


def execute_sa(payload: dict) -> str:
    """DistilBERT sentiment analysis (with jailbreak check)."""
    text = payload.get("text", "Default query.")
    if _check_jailbreak(text):
        raise RuntimeError("JailbreakBlocked: prompt injection detected, request denied")
    sa_model = ENGINES["SA"]
    results = sa_model(text)
    return f"{results[0]['label']} (score={results[0]['score']:.4f})"


def execute_sim(payload: dict) -> str:
    """SBERT semantic similarity (with jailbreak check)."""
    text1 = payload.get("text1", "")
    text2 = payload.get("text2", "")
    combined = f"{text1} {text2}"
    if _check_jailbreak(combined):
        raise RuntimeError("JailbreakBlocked: prompt injection detected, request denied")
    sim_model = ENGINES["SIM"]
    emb = sim_model.encode([text1, text2])
    score = float(np.dot(emb[0], emb[1]) / (np.linalg.norm(emb[0]) * np.linalg.norm(emb[1])))
    return json.dumps({"similarity": round(score, 4)})


def execute_sum(payload: dict) -> str:
    """t5-small abstractive summarization (with jailbreak check)."""
    text = payload.get("text", "")
    if _check_jailbreak(text):
        raise RuntimeError("JailbreakBlocked: prompt injection detected, request denied")
    t5_tok, t5_model = ENGINES["SUM"]
    prefix = "summarize: "
    inputs = t5_tok(prefix + text, return_tensors="pt", max_length=512, truncation=True)
    if DEVICE_ID >= 0:
        inputs = {k: v.to("cuda") for k, v in inputs.items()}
    summary_ids = t5_model.generate(inputs["input_ids"], max_length=60, num_beams=4,
                                     early_stopping=True)
    summary = t5_tok.decode(summary_ids[0], skip_special_tokens=True)
    return summary


EXECUTORS = {
    "IC":  execute_ic,
    "SA":  execute_sa,
    "SIM": execute_sim,
    "SUM": execute_sum,
}


# Main loop

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

    # recycle tasks this worker cannot handle (specialist mismatch)
    if task_type not in VALID_TYPES:
        logger.info("%s Cannot handle %s, recycling to %s.", WORKER_NAME, task_type, queue_name)
        r.lpush(queue_name, payload_str)
        continue

    stolen_tag = " (stolen)" if stolen else ""
    logger.info("%s Processing %s task %s%s", WORKER_NAME, task_type, task_id, stolen_tag)

    r.hset("active:tasks", task_id, "PROCESSING")
    start_time = time.time()
    model_output, final_status = "", "SUCCESS"

    try:
        executor = EXECUTORS.get(task_type)
        if executor is None:
            model_output = f"Unknown task_type: {task_type}"
            final_status = "FAILED"
        else:
            model_output = executor(payload)

    except Exception as e:
        err_msg = str(e)
        final_status = "FAILED"
        if "JailbreakBlocked" in err_msg:
            model_output = err_msg
        else:
            model_output = f"{type(e).__name__}: {err_msg}"
        logger.error("%s Task %s failed: %s", WORKER_NAME, task_id, model_output)
        traceback.print_exc()

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
