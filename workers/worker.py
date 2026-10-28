"""
Worker node — heterogeneous GPU/CPU inference with Redis task queue. v1.5
Registry — supports 4 task types across 5 workers.
"""

import json
import logging
import os
import random
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
from shared.task_registry import TASK_REGISTRY, PRIORITY_LEVELS
from shared.retry_logic import retry_delay as _retry_delay

REDIS_HOST = os.getenv("REDIS_HOST", "redis-broker")
WORKER_TYPE = os.getenv("WORKER_TYPE", "SLOW")
WORKER_NAME = os.getenv("WORKER_NAME",
    "worker-fast-gpu" if WORKER_TYPE == "FAST" else f"worker-cpu-{os.getpid()}")
API_GATEWAY = os.getenv("API_GATEWAY_URL", "http://fastapi-gateway:8000")
ENABLE_STEALING = os.getenv("ENABLE_WORK_STEALING", "1") == "1"
SCHED_MODE = os.getenv("SCHEDULING_STRATEGY", "fifo")
TASK_FAILURE_RATE = float(os.getenv("TASK_FAILURE_RATE", "0"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
RETRY_BASE_DELAY = float(os.getenv("RETRY_BASE_DELAY", "2.0"))
RETRY_MODE = os.getenv("RETRY_MODE", "backoff_jitter")  # none / immediate / backoff / backoff_jitter


def _fault_config():
    """Live fault config from Redis (Dashboard sliders), falling back to env."""
    try:
        rate = float(r.get("config:task_failure_rate") or TASK_FAILURE_RATE)
        retries = int(r.get("config:max_retries") or MAX_RETRIES)
        delay = float(r.get("config:retry_base_delay") or RETRY_BASE_DELAY)
        mode = r.get("config:retry_mode") or RETRY_MODE
        return rate, retries, delay, mode
    except Exception:
        return TASK_FAILURE_RATE, MAX_RETRIES, RETRY_BASE_DELAY, RETRY_MODE



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
                total_latency, waiting_time, retry_count, final_status, model_output, input_summary)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                audit_payload["task_id"], audit_payload["task_type"],
                audit_payload["worker_name"], audit_payload["execution_latency"],
                audit_payload.get("total_latency"), audit_payload.get("waiting_time"),
                audit_payload.get("retry_count", 0),
                audit_payload["final_status"], audit_payload["model_output"],
                audit_payload.get("input_summary", ""),
            ),
        )
        conn.commit()
        conn.close()
    except Exception as db_err:
        logger.error("%s SQLite fallback failed: %s", WORKER_NAME, db_err)


# shutdown

def shutdown(signum, frame):
    logger.info("%s Received signal %s, shutting down.", WORKER_NAME, signum)
    sys.exit(0)

signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)

# Queue pop with optional zset support

def _pop_queue(qname: str, zset: bool):
    """Pop with the primitive matching the mode.

    If the key still holds the *other* structure (stale after a mode switch,
    e.g. a leftover ZSET when the mode is now FIFO), fall back to the matching
    primitive to drain it rather than crashing the worker loop.
    """
    def _try(use_zset: bool):
        if use_zset:
            res = r.bzpopmin(qname, timeout=1)
            if res is None:
                return None
            return (res[0], res[1], res[2])
        res = r.blpop(qname, timeout=1)
        if res is None:
            return None
        return (res[0], res[1], None)

    try:
        return _try(zset)
    except redis.exceptions.ResponseError:
        # wrong structure — try the alternate primitive to drain stale tasks
        try:
            return _try(not zset)
        except redis.exceptions.ResponseError:
            # persistent mismatch; skip this iteration instead of crashing
            return None


# Delayed queue

def _enqueue(tt: str, payload_str: str):
    """Route a task back to its heavy/light queue using current queue_mode."""
    q = f"queue:{TASK_REGISTRY[tt]['queue']}"
    qm = r.get("config:queue_mode") or "fifo"
    if qm in ("priority", "setf"):
        try:
            p = json.loads(payload_str)
        except Exception:
            p = {}
        if qm == "priority":
            score = PRIORITY_LEVELS.get(p.get("priority", "MEDIUM"), 1)
        else:  # setf
            score = TASK_REGISTRY[tt].get("expected_time", 2.0)
        r.zadd(q, {payload_str: score})
    else:
        r.lpush(q, payload_str)
    return q


# Atomically claim all due delayed members (read + remove in one step) so two
# workers sweeping concurrently cannot both re-enqueue the same task.
_SWEEP_LUA = """
local due = redis.call('zrangebyscore', KEYS[1], 0, ARGV[1])
if #due > 0 then
    redis.call('zremrangebyscore', KEYS[1], 0, ARGV[1])
end
return due
"""


def _sweep_delayed():
    now = time.time()
    try:
        items = r.eval(_SWEEP_LUA, 1, "queue:delayed", now)
    except Exception as e:
        logger.warning("%s Delayed sweep eval failed: %s", WORKER_NAME, e)
        return
    if not items:
        return
    logger.info("%s Sweeping %d delayed task(s)", WORKER_NAME, len(items))
    for payload_str in items:
        try:
            p = json.loads(payload_str)
            tt = p["task_type"]
            _enqueue(tt, payload_str)
            r.delete(f"config:delay:{p['task_id']}")
        except Exception as e:
            logger.warning("%s Delayed sweep error: %s", WORKER_NAME, e)


def _replay_dead():
    """Replay oldest dead-letter task back to its queue, fresh (retry_count/first_attempt cleared)."""
    payload_str = r.rpop("queue:dead")
    if not payload_str:
        return
    try:
        p = json.loads(payload_str)
        p.pop("retry_count", None)           # replay = fresh submission
        p.pop("first_attempt_time", None)    # end-to-end latency resets too
        refreshed = json.dumps(p)
        _enqueue(p["task_type"], refreshed)
        logger.info("%s Replayed dead task %s", WORKER_NAME, p.get("task_id", "?"))
    except Exception as e:
        logger.warning("%s Replay error, returning to dead: %s", WORKER_NAME, e)
        r.rpush("queue:dead", payload_str)


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
    summary_ids = t5_model.generate(inputs["input_ids"], max_length=30, num_beams=1,
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

SIGNAL = shutdown


heartbeat_counter = 0
logger.info("%s Main loop started. sched=%s", WORKER_NAME, SCHED_MODE)

while True:
    task_data = None
    stolen = False
    zset_mode = SCHED_MODE in ("priority", "setf")
    # check for live queue_mode change from Redis
    live_mode = r.get("config:queue_mode")
    if live_mode and live_mode != SCHED_MODE:
        logger.info("%s queue_mode changed: %s -> %s", WORKER_NAME, SCHED_MODE, live_mode)
        SCHED_MODE = live_mode
        zset_mode = SCHED_MODE in ("priority", "setf")

    if WORKER_TYPE == "FAST":
        task_data = _pop_queue("queue:heavy", zset_mode)
        if task_data is None and SCHED_MODE != "setf":
            stealing_enabled = r.get("config:work_stealing") != "0"
            if stealing_enabled and ENABLE_STEALING:
                task_data = _pop_queue("queue:light", zset_mode)
                if task_data:
                    stolen = True
    else:
        task_data = _pop_queue("queue:light", zset_mode)

    if task_data is None:
        heartbeat_counter += 1
        if heartbeat_counter % 5 == 0:
            r.set(f"worker:{WORKER_NAME}:heartbeat", "alive", ex=15)
            _sweep_delayed()
            if r.get("config:replay_dead"):
                _replay_dead()
                r.delete("config:replay_dead")   # one replay per click
        continue

    r.set(f"worker:{WORKER_NAME}:heartbeat", "alive", ex=15)

    queue_name, payload_str, zscore = task_data
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
        if zscore is not None:
            r.zadd(queue_name, {payload_str: zscore})
        else:
            r.rpush(queue_name, payload_str)
        continue

    stolen_tag = " (stolen)" if stolen else ""
    logger.info("%s Processing %s task %s%s", WORKER_NAME, task_type, task_id, stolen_tag)

    r.hset("active:tasks", task_id, "PROCESSING")
    r.hset("active:tasks", f"{WORKER_NAME}_current", f"{task_id} ({task_type})")
    r.setex(f"lease:{task_id}", 200, WORKER_NAME)
    start_time = time.time()
    model_output, final_status = "", "SUCCESS"
    retry_count = payload.get("retry_count", 0)
    injected_failure = False

    fail_rate, max_retries, base_delay, retry_mode = _fault_config()

    # record first attempt time for end-to-end latency measurement
    if "first_attempt_time" not in payload:
        payload["first_attempt_time"] = time.time()

    # fault injection: simulate transient failure before inference
    if fail_rate > 0 and random.random() < fail_rate:
        injected_failure = True

    try:
        if injected_failure:
            raise RuntimeError(f"InjectedFailure: simulated transient error (attempt {retry_count + 1})")
        executor = EXECUTORS.get(task_type)
        if executor is None:
            model_output = f"Unknown task_type: {task_type}"
            final_status = "FAILED"
        else:
            model_output = executor(payload)

    except Exception as e:
        err_msg = str(e)
        # jailbreak is not a fault, no retry
        if "JailbreakBlocked" in err_msg:
            final_status = "FAILED"
            model_output = err_msg
        elif retry_mode == "none" or retry_count >= max_retries:
            # no-retry mode, or retries exhausted -> dead letter queue
            final_status = "FAILED"
            model_output = f"ExhaustedRetries: task failed after {retry_count} retries"
            r.lpush("queue:dead", json.dumps(payload))
            logger.info("%s Task %s -> dead letter queue (exhausted retries)", WORKER_NAME, task_id)
        else:
            # transient failure -> backoff (per mode) into delayed queue
            payload["retry_count"] = retry_count + 1
            delay = _retry_delay(retry_mode, payload["retry_count"], base_delay)
            r.zadd("queue:delayed", {json.dumps(payload): time.time() + delay})
            r.hdel("active:tasks", task_id)
            r.hdel("active:tasks", f"{WORKER_NAME}_current")
            r.delete(f"lease:{task_id}")
            logger.info("%s Retrying task %s in %.1fs (attempt %d/%d, mode=%s)",
                        WORKER_NAME, task_id, delay, payload["retry_count"], max_retries, retry_mode)
            continue

    latency = time.time() - start_time
    total_latency = time.time() - payload.get("first_attempt_time", start_time)
    waiting_time = start_time - payload.get("created_at", start_time)

    r.hdel("active:tasks", task_id)
    r.hdel("active:tasks", f"{WORKER_NAME}_current")
    r.delete(f"lease:{task_id}")

    audit_payload = {
        "task_id": task_id, "task_type": task_type,
        "worker_name": WORKER_NAME,
        "execution_latency": round(latency, 6),
        "total_latency": round(total_latency, 6),
        "waiting_time": round(max(0.0, waiting_time), 6),
        "retry_count": payload.get("retry_count", 0),
        "final_status": final_status, "model_output": model_output,
        "input_summary": payload.get("input_summary", ""),
    }
    write_audit(audit_payload)
    logger.info("%s Task %s -> %s (%.4fs)", WORKER_NAME, task_id, final_status, latency)

    heartbeat_counter += 1
    r.set(f"worker:{WORKER_NAME}:heartbeat", "alive", ex=15)
