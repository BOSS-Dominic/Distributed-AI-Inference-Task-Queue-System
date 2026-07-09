"""
FastAPI ingestion gateway — receives tasks, dispatches to Redis, writes audit logs to SQLite. v
"""

import json
import logging
import os
import sqlite3
import time
import uuid

import redis
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("gateway")

app = FastAPI(
    title="Distributed AI Task Queue - Ingestion Gateway",
    version="1.0",
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
    r.delete("queue:heavy", "queue:light", "active:tasks")
    for key in r.keys("worker:*:heartbeat"):
        r.delete(key)
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
    conn.commit()
    conn.close()


@app.post("/v1/inference/tasks")
async def ingest_task(
    task_type: str = Form(...),
    file: UploadFile = File(None),
    text_data: str = Form(None),
):
    task_id = str(uuid.uuid4())
    task_payload = {
        "task_id": task_id,
        "task_type": task_type,
        "created_at": time.time(),
    }

    if task_type == "CV" and file:
        ext = os.path.splitext(file.filename or "image.jpg")[1] or ".jpg"
        safe_ext = ext if ext.lower() in {".jpg", ".jpeg", ".png", ".webp", ".bmp"} else ".jpg"
        file_path = os.path.join(INPUTS_DIR, f"{task_id}{safe_ext}")

        content = await file.read()
        with open(file_path, "wb") as buffer:
            buffer.write(content)

        task_payload["image_path"] = file_path
        task_payload["input_summary"] = f"CV image: {file.filename} ({len(content)} bytes)"
        r.lpush("queue:heavy", json.dumps(task_payload))

    else:
        text = text_data or "Default benchmark query."
        task_payload["text"] = text
        task_payload["input_summary"] = f"NLP text: {text[:80]}..."
        r.lpush("queue:light", json.dumps(task_payload))

    r.hset("active:tasks", task_id, "PENDING")

    return JSONResponse(
        content={"status": "Accepted", "task_id": task_id},
        status_code=202,
    )


@app.post("/v1/internal/audit")
async def write_audit_log(data: dict):
    required_fields = ("task_id", "task_type", "worker_name",
                       "execution_latency", "final_status", "model_output")
    for field in required_fields:
        if field not in data:
            return JSONResponse(
                content={"status": "error", "detail": f"Missing field: {field}"},
                status_code=422,
            )

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute(
            """INSERT OR REPLACE INTO task_history
               (task_id, task_type, worker_name, execution_latency,
                final_status, model_output, input_summary)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                data["task_id"],
                data["task_type"],
                data["worker_name"],
                data["execution_latency"],
                data["final_status"],
                data["model_output"],
                data.get("input_summary", ""),
            ),
        )
        conn.commit()
        conn.close()
        return {"status": "logged"}
    except Exception as e:
        logger.error("Audit write failed: %s", e)
        return JSONResponse(
            content={"status": "error", "detail": str(e)},
            status_code=500,
        )


@app.get("/v1/queue/status")
def queue_status():
    return {
        "queue_heavy": r.llen("queue:heavy"),
        "queue_light": r.llen("queue:light"),
        "active_tasks": r.hlen("active:tasks"),
        "timestamp": time.time(),
    }


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
