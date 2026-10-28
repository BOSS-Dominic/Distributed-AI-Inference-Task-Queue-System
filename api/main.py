"""
FastAPI ingestion gateway — receives tasks, dispatches to Redis, writes audit logs to SQLite. v1.5
"""

import json
import logging
import os
import sqlite3
import time
import uuid

import redis
from fastapi import FastAPI, File, Form, UploadFile, BackgroundTasks
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("gateway")

from shared.task_registry import TASK_REGISTRY, PRIORITY_LEVELS, DEFAULT_PRIORITY

app = FastAPI(
    title="Distributed AI Task Queue - Ingestion Gateway",
    version="1.5",
)

# --- Business metrics (scraped by Prometheus: http://fastapi-gateway:8000/metrics)
TASKS_SUBMITTED = Counter(
    "gateway_tasks_submitted_total",
    "Tasks accepted by the gateway",
    ["task_type", "priority", "delayed"],
)
TASKS_AUDITED = Counter(
    "gateway_tasks_audited_total",
    "Task completions written to the audit trail",
    ["task_type", "status"],
)
TASK_LATENCY = Histogram(
    "gateway_task_latency_seconds",
    "End-to-end task latency reported by workers",
    ["task_type"],
    buckets=(0.1, 0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300),
)
QUEUE_DEPTH = Gauge(
    "gateway_queue_depth",
    "Current queue depth",
    ["queue"],
)

r = redis.Redis(
    host=os.getenv("REDIS_HOST", "redis-broker"),
    port=6379,
    db=0,
    decode_responses=True,
    socket_connect_timeout=5,
    socket_keepalive=True,
)

STORAGE_DIR = "/app/storage"
INPUTS_DIR = os.path.join(STORAGE_DIR, "inputs")
DB_PATH = os.path.join(STORAGE_DIR, "metrics.db")


@app.on_event("startup")
def startup():
    os.makedirs(INPUTS_DIR, exist_ok=True)
    _init_db()
    r.delete("queue:heavy", "queue:light", "queue:delayed", "active:tasks")
    for key in r.keys("worker:*:heartbeat"):
        r.delete(key)
    if r.get("config:queue_mode") is None:
        r.set("config:queue_mode", "fifo")
    print("[Gateway] Startup complete. SQLite WAL mode active. Redis connected.")


def _init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_history (
            task_id        TEXT PRIMARY KEY,
            task_type      TEXT NOT NULL,
            worker_name    TEXT,
            execution_latency REAL,
            total_latency  REAL,
            waiting_time   REAL,
            retry_count    INTEGER DEFAULT 0,
            final_status   TEXT NOT NULL,
            model_output   TEXT,
            input_summary  TEXT,
            timestamp      DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_task_history_timestamp
        ON task_history(timestamp DESC)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_task_history_status
        ON task_history(final_status)
    """)
    # migration: add columns if this DB predates v1.6 (retry tracking)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(task_history)")}
    if "retry_count" not in cols:
        conn.execute("ALTER TABLE task_history ADD COLUMN retry_count INTEGER DEFAULT 0")
    if "total_latency" not in cols:
        conn.execute("ALTER TABLE task_history ADD COLUMN total_latency REAL")
    if "waiting_time" not in cols:
        conn.execute("ALTER TABLE task_history ADD COLUMN waiting_time REAL")
    conn.commit()
    conn.close()


@app.post("/v1/inference/tasks")
async def ingest_task(
    task_type: str = Form(...),
    file: UploadFile = File(None),
    text_data: str = Form(None),
    text_data_2: str = Form(None),
    priority: str = Form(DEFAULT_PRIORITY),
    scheduled_at: str = Form(None),
):
    if task_type not in TASK_REGISTRY:
        return JSONResponse(
            content={"status": "error", "detail": f"Unknown task_type: {task_type}"},
            status_code=400,
        )
    if priority not in PRIORITY_LEVELS:
        return JSONResponse(
            content={"status": "error", "detail": f"Unknown priority: {priority}"},
            status_code=400,
        )

    R = TASK_REGISTRY[task_type]
    task_id = str(uuid.uuid4())
    task_payload = {
        "task_id": task_id,
        "task_type": task_type,
        "priority": priority,
        "created_at": time.time(),
    }

    if R["requires_file"] and file:
        ext = os.path.splitext(file.filename or "image.jpg")[1] or ".jpg"
        safe_ext = ext if ext.lower() in {".jpg", ".jpeg", ".png", ".webp", ".bmp"} else ".jpg"
        file_path = os.path.join(INPUTS_DIR, f"{task_id}{safe_ext}")

        content = await file.read()
        with open(file_path, "wb") as buffer:
            buffer.write(content)

        task_payload["image_path"] = file_path
        task_payload["input_summary"] = f"{task_type} image: {file.filename} ({len(content)} bytes)"

    elif R.get("requires_text_pair"):
        text1 = text_data or "The weather is nice today."
        text2 = text_data_2 or "It is a beautiful day outside."
        task_payload["text1"] = text1
        task_payload["text2"] = text2
        task_payload["input_summary"] = f"{task_type} pair: '{text1[:30]}...' vs '{text2[:30]}...'"

    else:
        text = text_data or "Default benchmark query."
        task_payload["text"] = text
        task_payload["input_summary"] = f"{task_type} text: {text[:80]}..."

    queue_name = f"queue:{R['queue']}"
    payload_str = json.dumps(task_payload)

    # client-scheduled task: delay via queue:delayed (seconds offset)
    if scheduled_at:
        try:
            delay_s = float(scheduled_at)
        except ValueError:
            return JSONResponse(
                content={"status": "error", "detail": "scheduled_at must be a number of seconds from now"},
                status_code=400,
            )
        r.zadd("queue:delayed", {payload_str: time.time() + delay_s})
        r.hset("active:tasks", task_id, "PENDING")
        TASKS_SUBMITTED.labels(task_type, priority, "true").inc()
        return JSONResponse(
            content={"status": "Accepted", "task_id": task_id, "delayed": True},
            status_code=202,
        )

# Enqueue

    queue_mode = r.get("config:queue_mode") or "fifo"

    if queue_mode == "priority":
        score = PRIORITY_LEVELS.get(priority, 1)
        r.zadd(queue_name, {payload_str: score})
    elif queue_mode == "setf":
        et = TASK_REGISTRY[task_type].get("expected_time", 2.0)
        r.zadd(queue_name, {payload_str: et})
    else:
        r.lpush(queue_name, payload_str)

    r.hset("active:tasks", task_id, "PENDING")
    TASKS_SUBMITTED.labels(task_type, priority, "false").inc()

    return JSONResponse(
        content={"status": "Accepted", "task_id": task_id},
        status_code=202,
    )


@app.post("/v1/internal/audit")
async def write_audit_log(data: dict, background_tasks: BackgroundTasks):
    required_fields = ("task_id", "task_type", "worker_name",
                       "execution_latency", "final_status", "model_output")
    for field in required_fields:
        if field not in data:
            return JSONResponse(
                content={"status": "error", "detail": f"Missing field: {field}"},
                status_code=422,
            )
    task_type = str(data.get("task_type", "?"))
    status = str(data.get("final_status", "?"))
    TASKS_AUDITED.labels(task_type, status).inc()
    latency = data.get("total_latency")
    if latency is None:
        latency = data.get("execution_latency")
    if latency is not None:
        TASK_LATENCY.labels(task_type).observe(float(latency))
    background_tasks.add_task(_write_to_sqlite, data)
    return {"status": "accepted"}


def _write_to_sqlite(data: dict):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute(
            """INSERT OR REPLACE INTO task_history
               (task_id, task_type, worker_name, execution_latency,
                total_latency, waiting_time, retry_count, final_status, model_output, input_summary)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data["task_id"],
                data["task_type"],
                data["worker_name"],
                data["execution_latency"],
                data.get("total_latency"),
                data.get("waiting_time"),
                data.get("retry_count", 0),
                data["final_status"],
                data["model_output"],
                data.get("input_summary", ""),
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error("Audit write failed: %s", e)


@app.get("/v1/queue/status")
def queue_status():
    qm = r.get("config:queue_mode") or "fifo"
    if qm in ("priority", "setf"):
        heavy = r.zcard("queue:heavy")
        light = r.zcard("queue:light")
    else:
        heavy = r.llen("queue:heavy")
        light = r.llen("queue:light")
    return {
        "queue_heavy": heavy,
        "queue_light": light,
        "queue_delayed": r.zcard("queue:delayed"),
        "active_tasks": r.hlen("active:tasks"),
        "timestamp": time.time(),
    }


def _refresh_queue_gauges():
    try:
        qm = r.get("config:queue_mode") or "fifo"
        if qm in ("priority", "setf"):
            heavy, light = r.zcard("queue:heavy"), r.zcard("queue:light")
        else:
            heavy, light = r.llen("queue:heavy"), r.llen("queue:light")
        QUEUE_DEPTH.labels("heavy").set(heavy)
        QUEUE_DEPTH.labels("light").set(light)
        QUEUE_DEPTH.labels("delayed").set(r.zcard("queue:delayed"))
        QUEUE_DEPTH.labels("dead").set(r.llen("queue:dead"))
    except Exception:
        pass


@app.get("/metrics")
def metrics():
    _refresh_queue_gauges()
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/health")
def health():
    redis_ok = False
    try:
        redis_ok = r.ping()
    except Exception:
        pass

    sqlite_ok = False
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        conn.execute("SELECT 1 FROM task_history LIMIT 1")
        conn.close()
        sqlite_ok = True
    except Exception:
        sqlite_ok = os.path.exists(DB_PATH)

    return {
        "status": "ok" if (redis_ok and sqlite_ok) else "degraded",
        "redis": redis_ok,
        "sqlite": sqlite_ok,
    }
