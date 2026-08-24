#!/usr/bin/env python3
"""
Automated experiment runner for the distributed AI inference task queue.

Experiments:
  exp1  Retry strategies        (4 modes x reps, failure 30%, base delay 2s)
  exp2  Scheduling strategies   (fifo / priority / setf x reps, failure 0)
  exp2b Work-stealing           (fifo: GPU stealing ON vs OFF x reps)
  exp3  Resource specialisation (specialists on/off x reps, fifo)
  exp4  Scalability             (lambda 0.5..30 sweep x reps, setf)
  exp5  Adversarial robustness  (jailbreak 10/20/30% x reps, fifo)

Work-stealing policy (runtime is ~5x slower with stealing OFF because light
tasks stay on CPU):
  OFF: exp2 (fair scheduling comparison), exp2b (measured explicitly),
       exp3 (specialist benefit must not be masked by the GPU),
       exp4 (SETF forces it off anyway)
  ON:  exp1 (success rate / retry_count unaffected by which worker runs),
       exp5 (detection is worker-independent)

Usage:
  python scripts/run_experiments.py --exp 1
  python scripts/run_experiments.py --exp 2 --reps 3
  python scripts/run_experiments.py --exp all
  python scripts/run_experiments.py --exp 4 --dry-run     # print plan only

Requirements:
  docker compose up -d  (all containers healthy)
  pip install redis requests pandas
  run from the repository root.

Output: experiments/exp{1,2,2b,3,4,5}/*.csv + experiments/manifest.csv
        (charts are generated separately by scripts/plot_experiments.py)
"""

import argparse
import csv
import glob
import json
import os
import random
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime

import pandas as pd
import redis
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
API = os.getenv("API_GATEWAY_URL", "http://localhost:8000")
DB_PATH = os.path.join(ROOT, "storage", "metrics.db")
OUT_DIR = os.path.join(ROOT, "experiments")
PROMPTS_FILE = os.path.join(ROOT, "storage", "inputs", "prompts.txt")
GENERATOR_CONTAINER = "workload-generator"
SPECIALISTS = ["worker-sim-specialist", "worker-sum-specialist"]

TASKS = 100
LAMBDA = 20.0
REPS = 5

r = redis.Redis(host=REDIS_HOST, port=6379, db=0, decode_responses=True)


# ---------------- helpers ----------------

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def set_config(**kwargs):
    """Write live config keys to Redis; workers adapt within ~1s."""
    for k, v in kwargs.items():
        r.set(f"config:{k}", str(v))


def flush_queues(dead_too=False):
    keys = ["queue:heavy", "queue:light", "queue:delayed", "active:tasks"]
    if dead_too:
        keys.append("queue:dead")
    r.delete(*keys)


def purge_history():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout=15000;")
    conn.execute("DELETE FROM task_history;")
    conn.commit()
    conn.close()


def queue_depth():
    qm = r.get("config:queue_mode") or "fifo"
    if qm in ("priority", "setf"):
        heavy = r.zcard("queue:heavy")
        light = r.zcard("queue:light")
    else:
        heavy = r.llen("queue:heavy")
        light = r.llen("queue:light")
    return heavy + light + r.zcard("queue:delayed") + r.hlen("active:tasks")


_orphan_seen = {}


def _queued():
    qm = r.get("config:queue_mode") or "fifo"
    if qm in ("priority", "setf"):
        return r.zcard("queue:heavy") + r.zcard("queue:light") + r.zcard("queue:delayed")
    return r.llen("queue:heavy") + r.llen("queue:light") + r.zcard("queue:delayed")


def _clear_orphans(grace=60):
    """Remove active:tasks markers that carry no lease and never get worked on.
    Only runs when the queues are EMPTY: while tasks are still queued, PENDING
    markers are legitimate (waiting to be popped) and must NOT be cleared.
    A healthy in-flight task always holds a lease within milliseconds of being
    popped, so a lease-less marker persisting past `grace` after the queue drains
    is an orphan left by a dead/restarted worker."""
    global _orphan_seen
    if _queued() > 0:
        return  # tasks still queued -> PENDING markers are legit, don't touch
    active = r.hgetall("active:tasks")
    now = time.time()
    for tid in active:
        if tid.startswith("worker"):
            continue
        if r.exists(f"lease:{tid}"):
            _orphan_seen.pop(tid, None)
            continue
        if tid not in _orphan_seen:
            _orphan_seen[tid] = now
        elif now - _orphan_seen[tid] > grace:
            log(f"  clearing orphan active marker {tid[:8]} ({active[tid]})")
            r.hdel("active:tasks", tid)
            _orphan_seen.pop(tid, None)


def system_busy():
    """Real work that could still land an audit: queued tasks + in-flight tasks
    that carry a lease. Lease-less orphan markers do NOT count, so a dead worker's
    leftover marker no longer blocks a run."""
    qm = r.get("config:queue_mode") or "fifo"
    if qm in ("priority", "setf"):
        queued = r.zcard("queue:heavy") + r.zcard("queue:light")
    else:
        queued = r.llen("queue:heavy") + r.llen("queue:light")
    queued += r.zcard("queue:delayed")
    active = r.hgetall("active:tasks")
    leased = sum(1 for t in active if not t.startswith("worker") and r.exists(f"lease:{t}"))
    return queued + leased


def wait_idle(timeout=2400, poll=5, orphan_grace=60):
    """Wait until queues and in-flight markers are drained (dead queue ignored).
    Orphan active markers (no lease) are cleared after `orphan_grace` seconds so a
    crashed worker's leftover cannot hang the batch."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        _clear_orphans(orphan_grace)
        if queue_depth() == 0:
            return True
        time.sleep(poll)
    raise RuntimeError(f"Timed out waiting for drain (depth={queue_depth()})")


def wait_for_workers(names, timeout=240, online=True):
    """Wait until the given workers start (online=True) or stop heartbeating."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        alive = [n for n in names if r.exists(f"worker:{n}:heartbeat")]
        if online and len(alive) == len(names):
            return
        if not online and not alive:
            return
        time.sleep(3)
    raise RuntimeError(f"Workers not {'online' if online else 'offline'}: {names}")


def trigger_generator(total, rate, mix=None):
    """Inject via the workload-generator container. If no explicit mix is
    given, split total evenly across the four types (the generator IGNORES
    the total arg when explicit per-type counts are supplied)."""
    if mix is None:
        base = total // 4
        mix = {"ic": base, "sa": base, "sim": base, "sum_": total - 3 * base}
    # kill any orphaned generator left by an aborted run before injecting,
    # otherwise its late submissions contaminate this run's CSV.
    # (pkill is absent in the slim image, so scan /proc via python.)
    _kill_gen = ("import os,signal;"
                 "[os.kill(int(p),signal.SIGKILL) for p in os.listdir('/proc') if p.isdigit() "
                 "and 'generator.py' in open('/proc/'+p+'/cmdline','rb').read().decode('utf-8','replace')]")
    try:
        subprocess.run(["docker", "exec", GENERATOR_CONTAINER, "python", "-c", _kill_gen],
                       capture_output=True, text=True, timeout=30)
        time.sleep(2)
    except Exception:
        pass
    cmd = ["docker", "exec", GENERATOR_CONTAINER, "python", "generator.py",
           str(total), str(rate), str(mix["ic"]), str(mix["sa"]),
           str(mix["sim"]), str(mix["sum_"])]
    # injection takes ~total/rate seconds (Poisson); budget a generous margin
    # so low-λ runs (e.g. 0.05 -> ~33 min) are not killed by the subprocess timeout.
    injection_est = total / rate if rate > 0 else 0
    timeout = int(max(900, injection_est * 1.5 + 120))
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if res.returncode != 0:
        raise RuntimeError(f"Generator failed: {res.stderr[-500:]}")
    return res.stdout


def load_prompts():
    if os.path.isfile(PROMPTS_FILE):
        with open(PROMPTS_FILE, encoding="utf-8") as f:
            lines = [ln.strip() for ln in f if ln.strip()]
            if lines:
                return lines
    return [
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


def inject_direct(total, rate, jailbreak_density):
    """Poisson HTTP injection with a controlled jailbreak fraction (exp 5)."""
    from shared.jailbreak_words import JAILBREAK_PAYLOADS
    prompts = load_prompts()
    types = ["SA", "SIM", "SUM"]  # text-only, no IC (matches exp 5)
    submitted = 0
    for i in range(total):
        tt = types[i % 3]
        text1 = random.choice(prompts)
        if random.random() < jailbreak_density:
            text1 = random.choice(JAILBREAK_PAYLOADS)
        if tt == "SIM":
            data = {"task_type": "SIM", "text_data": text1,
                    "text_data_2": random.choice(prompts), "priority": "MEDIUM"}
        else:
            data = {"task_type": tt, "text_data": text1, "priority": "MEDIUM"}
        try:
            resp = requests.post(f"{API}/v1/inference/tasks", data=data, timeout=10)
            if resp.status_code == 202:
                submitted += 1
        except requests.RequestException:
            pass
        if i < total - 1:
            time.sleep(random.expovariate(rate))
    return submitted


def export_csv(exp, tag):
    conn = sqlite3.connect(DB_PATH, timeout=30)
    df = pd.read_sql_query("SELECT * FROM task_history ORDER BY timestamp", conn)
    conn.close()
    out = os.path.join(OUT_DIR, exp)  # exp is already "exp1"/"exp2b"/... (folder key)
    os.makedirs(out, exist_ok=True)
    path = os.path.join(out, f"{tag}.csv")
    df.to_csv(path, index=False)
    return path, len(df)


def record_manifest(exp, tag, config, csv_path, rows):
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, "manifest.csv")
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["timestamp", "exp", "tag", "config", "csv", "rows"])
        w.writerow([datetime.now().isoformat(timespec="seconds"), exp, tag,
                    json.dumps(config, ensure_ascii=False), csv_path, rows])


def run_batch(exp, tag, total=TASKS, rate=LAMBDA, direct=None, config=None):
    # Safety: never purge while real work could still land an audit — otherwise
    # their late writes land after the purge and contaminate this run. Lease-less
    # orphan markers from dead workers don't count as real work.
    if system_busy():
        raise RuntimeError(
            f"System not idle before {tag} (busy_work={system_busy()}). "
            "Wait for full drain, then re-run. Refusing to purge to avoid data contamination.")
    purge_history()
    flush_queues()
    time.sleep(2)
    log(f"  >> injecting {total} tasks @ lambda={rate}  (tag={tag})")
    if direct is not None:
        n = inject_direct(total, rate, direct)
        log(f"     direct injection submitted {n}/{total}")
    else:
        trigger_generator(total, rate)
    wait_idle()
    time.sleep(5)  # let the async audit flush to SQLite
    path, nrows = export_csv(exp, tag)
    record_manifest(exp, tag, config or {}, path, nrows)
    log(f"     DONE {tag}: {nrows} rows -> {os.path.relpath(path, ROOT)}")
    return nrows


def check_environment():
    subprocess.run(["docker", "exec", "redis-broker", "redis-cli", "ping"],
                   check=True, capture_output=True, text=True)
    subprocess.run(["docker", "exec", GENERATOR_CONTAINER, "python", "-c", "print('gen-ok')"],
                   check=True, capture_output=True, text=True, timeout=60)
    log("environment OK (redis + generator reachable)")


# ---------------- experiments ----------------

def next_run(exp, prefix, reps):
    """Find the first missing run number for a condition (e.g. 'exp2_fifo').
    If runs 1..3 already exist, returns 4 so the next run is 4; a brand-new
    condition (e.g. lambda 0.05) returns 1 and runs the full 1..reps."""
    existing = glob.glob(os.path.join(OUT_DIR, exp, f"{prefix}_run*.csv"))
    max_run = 0
    for f in existing:
        m = re.search(r"_run(\d+)\.csv$", os.path.basename(f))
        if m:
            max_run = max(max_run, int(m.group(1)))
    return max_run + 1


def exp1(reps, total=TASKS):
    log("=== Exp 1: retry strategies (fifo, failure 30%, base delay 2s, steal ON) ===")
    flush_queues(dead_too=True)
    modes = [("backoff_jitter", 3), ("backoff", 3), ("immediate", 3), ("none", 0)]
    for mode, retries in modes:
        for run in range(next_run("exp1", f"exp1_{mode}", reps), reps + 1):
            cfg = {"queue_mode": "fifo", "work_stealing": 1, "task_failure_rate": 0.30,
                   "max_retries": retries, "retry_base_delay": 2, "retry_mode": mode}
            set_config(**cfg)
            run_batch("exp1", f"exp1_{mode}_run{run}", total=total, rate=LAMBDA, config=cfg)
            time.sleep(3)


def exp2(reps, total=TASKS):
    log("=== Exp 2: scheduling strategies (failure 0, work-stealing OFF) ===")
    flush_queues(dead_too=True)
    for mode in ["fifo", "priority", "setf"]:
        for run in range(next_run("exp2", f"exp2_{mode}", reps), reps + 1):
            cfg = {"queue_mode": mode, "work_stealing": 0, "task_failure_rate": 0}
            set_config(**cfg)
            time.sleep(2)  # workers re-read mode
            run_batch("exp2", f"exp2_{mode}_run{run}", total=total, rate=LAMBDA, config=cfg)
            time.sleep(3)


def exp2b(reps, total=TASKS):
    """Work-stealing sub-experiment: FIFO with GPU stealing ON vs OFF.
    Light-queue waiting_time / throughput is compared (SETF can't steal by design)."""
    log("=== Exp 2b: work stealing (fifo, failure 0) ON vs OFF ===")
    flush_queues(dead_too=True)
    for steal in ["1", "0"]:
        for run in range(next_run("exp2b", f"exp2b_steal{steal}", reps), reps + 1):
            cfg = {"queue_mode": "fifo", "work_stealing": steal, "task_failure_rate": 0}
            set_config(**cfg)
            time.sleep(2)
            run_batch("exp2b", f"exp2b_steal{steal}_run{run}", total=total, rate=LAMBDA, config=cfg)
            time.sleep(3)


def exp3(reps, total=TASKS):
    log("=== Exp 3: resource specialisation (fifo, failure 0) ===")
    flush_queues(dead_too=True)
    base_start = next_run("exp3", "exp3_base", reps)
    spec_start = next_run("exp3", "exp3_specialist", reps)
    if base_start <= reps:
        # baseline: stop specialists (effect = 1 GPU + 2 CPU generalists only)
        log("stopping specialists for baseline...")
        subprocess.run(["docker", "compose", "stop"] + SPECIALISTS, cwd=ROOT, check=True)
        wait_for_workers(SPECIALISTS, online=False, timeout=120)
        for run in range(base_start, reps + 1):
            cfg = {"queue_mode": "fifo", "work_stealing": 0, "task_failure_rate": 0}
            set_config(**cfg)
            run_batch("exp3", f"exp3_base_run{run}", total=total, rate=LAMBDA, config=cfg)
            time.sleep(3)
    if spec_start <= reps:
        # specialist version: restart them
        log("restarting specialists...")
        subprocess.run(["docker", "compose", "start"] + SPECIALISTS, cwd=ROOT, check=True)
        wait_for_workers(SPECIALISTS, online=True, timeout=300)
        for run in range(spec_start, reps + 1):
            cfg = {"queue_mode": "fifo", "work_stealing": 0, "task_failure_rate": 0}
            set_config(**cfg)
            run_batch("exp3", f"exp3_specialist_run{run}", total=total, rate=LAMBDA, config=cfg)
            time.sleep(3)


def exp4(reps, total=TASKS):
    log("=== Exp 4: scalability (setf, failure 0) ===")
    flush_queues(dead_too=True)
    for lam in [0.05, 0.1, 0.5, 1, 2, 4, 8, 16, 30]:
        for run in range(next_run("exp4", f"exp4_lambda{lam}", reps), reps + 1):
            cfg = {"queue_mode": "setf", "work_stealing": 0, "task_failure_rate": 0, "lambda": lam}
            set_config(queue_mode="setf", work_stealing=0, task_failure_rate=0)
            run_batch("exp4", f"exp4_lambda{lam}_run{run}", total=total, rate=lam, config=cfg)
            time.sleep(3)


def exp5(reps, total=TASKS):
    log("=== Exp 5: adversarial robustness (fifo, failure 0, steal ON) ===")
    flush_queues(dead_too=True)
    for density in [0.10, 0.20, 0.30]:
        for run in range(next_run("exp5", f"exp5_jb{int(density * 100)}", reps), reps + 1):
            cfg = {"queue_mode": "fifo", "work_stealing": 1, "task_failure_rate": 0,
                   "jailbreak_density": density}
            set_config(queue_mode="fifo", work_stealing=1, task_failure_rate=0)
            run_batch("exp5", f"exp5_jb{int(density * 100)}_run{run}",
                      total=total, rate=LAMBDA, direct=density, config=cfg)
            time.sleep(3)


EXPERIMENTS = {"1": exp1, "2": exp2, "2b": exp2b, "3": exp3, "4": exp4, "5": exp5}


# ---------------- CLI ----------------

def print_plan(args):
    print(f"=== DRY RUN: target {args.reps} reps, {args.tasks} tasks/run (shows only missing runs) ===")
    plan = {
        "1": [(f"exp1_{m}", LAMBDA) for m, _ in
              [("backoff_jitter", 3), ("backoff", 3), ("immediate", 3), ("none", 0)]],
        "2": [(f"exp2_{m}", LAMBDA) for m in ["fifo", "priority", "setf"]],
        "2b": [(f"exp2b_steal{s}", LAMBDA) for s in ["1", "0"]],
        "3": [(f"exp3_{t}", LAMBDA) for t in ["base", "specialist"]],
        "4": [(f"exp4_lambda{l}", l) for l in [0.05, 0.1, 0.5, 1, 2, 4, 8, 16, 30]],
        "5": [(f"exp5_jb{d}", LAMBDA) for d in [10, 20, 30]],
    }
    total = 0
    for exp in (list(EXPERIMENTS) if args.exp == "all" else [args.exp]):
        folder = "exp" + exp
        for prefix, lam in plan[exp]:
            for run in range(next_run(folder, prefix, args.reps), args.reps + 1):
                print(f"  exp{exp}: {prefix}_run{run}  lambda={lam}")
                total += 1
    print(f"  -> {total} runs to execute")
    print("--- dry run only; nothing executed ---")


def main():
    ap = argparse.ArgumentParser(description="Automated experiment runner")
    ap.add_argument("--exp", choices=["1", "2", "2b", "3", "4", "5", "all"], default="all")
    ap.add_argument("--reps", type=int, default=REPS)
    ap.add_argument("--tasks", type=int, default=TASKS, help="tasks per run")
    ap.add_argument("--dry-run", action="store_true", help="print plan without running")
    args = ap.parse_args()

    if args.dry_run:
        print_plan(args)
        return

    check_environment()
    log(f"starting experiments: {args.exp}  ({args.reps} reps, {args.tasks} tasks/run)")
    os.makedirs(OUT_DIR, exist_ok=True)
    if args.exp == "all":
        for i in ["1", "2", "2b", "3", "4", "5"]:
            EXPERIMENTS[i](args.reps, total=args.tasks)
    else:
        EXPERIMENTS[args.exp](args.reps, total=args.tasks)
    log("ALL EXPERIMENTS COMPLETE")


if __name__ == "__main__":
    main()
