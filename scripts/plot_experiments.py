#!/usr/bin/env python3
"""
Generate charts from experiment CSVs.

Reads experiments/exp{1,2,2b,3,4,5}/*.csv (produced by run_experiments.py) and
writes charts + a per-condition summary CSV into each experiment's charts/ dir.

Usage:
  python scripts/plot_experiments.py                 # all experiments
  python scripts/plot_experiments.py --exp 2         # one experiment
  python scripts/plot_experiments.py --exp all --out /some/where
"""

import argparse
import glob
import os
import statistics

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "experiments")

# Only include run numbers <= MAX_RUN (e.g. --max-run 3 -> use runs 1..3).
MAX_RUN = 5

# ---- style --------------------------------------------------------------
plt.rcParams.update({
    "figure.dpi": 110,
    "savefig.dpi": 200,
    "font.size": 10,
    "axes.titlesize": 12,
    "axes.labelsize": 10,
    "axes.grid": True,
    "grid.alpha": 0.3,
    "legend.frameon": False,
    "axes.spines.top": False,
    "axes.spines.right": False,
})
MODE_COLORS = {"backoff_jitter": "#2f81f7", "backoff": "#58a6ff", "immediate": "#d29922", "none": "#f85149"}
STRAT_COLORS = {"fifo": "#2f81f7", "priority": "#d29922", "setf": "#3fb950"}
TYPES = ["IC", "SA", "SIM", "SUM"]


def out_dir(exp):
    d = os.path.join(DATA, f"exp{exp}", "charts")
    os.makedirs(d, exist_ok=True)
    return d


def load_condition(exp, prefixes):
    """Return {prefix: [DataFrame per run]} for all matching exp{exp}/{prefix}_runN.csv
    with N <= MAX_RUN (so --max-run 3 uses only runs 1..3)."""
    import re as _re
    base = os.path.join(DATA, f"exp{exp}")
    result = {}
    for p in prefixes:
        files = sorted(glob.glob(os.path.join(base, f"{p}_run*.csv")))
        frames = []
        for f in files:
            m = _re.search(r"_run(\d+)\.csv$", os.path.basename(f))
            if m and int(m.group(1)) > MAX_RUN:
                continue
            try:
                frames.append(pd.read_csv(f))
            except Exception:
                pass
        if frames:
            result[p] = frames
    return result


def is_jailbreak(df):
    return df["model_output"].astype(str).str.contains("Jailbreak", na=False)


def success_rate(df):
    nb = df[~is_jailbreak(df)]
    return float((nb["final_status"] == "SUCCESS").mean()) if len(nb) else float("nan")


def run_stats(frames, col):
    rows = []
    for df in frames:
        v = pd.to_numeric(df[col], errors="coerce").dropna()
        if v.empty:
            continue
        rows.append(dict(n=len(v), p50=v.median(), p95=v.quantile(0.95),
                         p99=v.quantile(0.99), mean=v.mean()))
    return pd.DataFrame(rows)


def save_summary(exp, df, name="summary"):
    df.to_csv(os.path.join(out_dir(exp), f"{name}.csv"), index=False)
    print(f"  {exp}: wrote {name}.csv")


def finish(exp, fig, name):
    fig.tight_layout()
    fig.savefig(os.path.join(out_dir(exp), name), bbox_inches="tight")
    plt.close(fig)
    print(f"  {exp}: wrote {name}")


# ---- exp1: retry strategies ---------------------------------------------
def plot_exp1():
    exp = "1"
    prefixes = ["exp1_backoff_jitter", "exp1_backoff", "exp1_immediate", "exp1_none"]
    cond = load_condition(exp, prefixes)
    if not cond:
        print("  exp1: no data, skipping"); return
    labels = [p.replace("exp1_", "") for p in prefixes]

    # success rate (jailbreak excluded) per run, averaged
    sr = []
    for p in prefixes:
        vals = [success_rate(df) for df in cond.get(p, [])]
        sr.append((statistics.mean(vals), statistics.stdev(vals) if len(vals) > 1 else 0))
    fig, ax = plt.subplots(figsize=(6, 3.6))
    xs = range(len(prefixes))
    ax.bar(xs, [v[0] for v in sr], yerr=[v[1] for v in sr], capsize=4,
           color=[MODE_COLORS.get(l, "#8b949e") for l in labels])
    ax.set_xticks(list(xs)); ax.set_xticklabels(labels, rotation=20, ha="right")
    ax.set_ylabel("Success rate (jailbreak excluded)")
    ax.set_ylim(0, 1.1)
    for i, v in enumerate(sr):
        ax.text(i, v[0] + 0.03, f"{v[0]*100:.0f}%", ha="center", fontsize=9)
    finish(exp, fig, "success_rate.png")

    # P95 total_latency per mode
    fig, ax = plt.subplots(figsize=(6, 3.6))
    for i, p in enumerate(prefixes):
        st = run_stats(cond.get(p, []), "total_latency")
        if st.empty:
            continue
        ax.bar(i, st["p95"].mean(), color=MODE_COLORS.get(labels[i], "#8b949e"),
               yerr=st["p95"].std() if len(st) > 1 else 0, capsize=4)
    ax.set_xticks(range(len(prefixes))); ax.set_xticklabels(labels, rotation=20, ha="right")
    ax.set_ylabel("P95 total latency (s)")
    finish(exp, fig, "p95_latency.png")

    # CDF of total_latency (success tasks, all runs combined)
    fig, ax = plt.subplots(figsize=(6, 4))
    for p, l in zip(prefixes, labels):
        vals = []
        for df in cond.get(p, []):
            s = df[(df["final_status"] == "SUCCESS") & ~is_jailbreak(df)]
            vals += pd.to_numeric(s["total_latency"], errors="coerce").dropna().tolist()
        vals = sorted(vals)
        if not vals:
            continue
        ax.plot(vals, [i / len(vals) for i in range(len(vals))],
                label=l, color=MODE_COLORS.get(l, "#8b949e"), lw=1.8)
    ax.set_xlabel("Total latency (s)"); ax.set_ylabel("CDF")
    ax.legend()
    ax.set_xlim(0, min(ax.get_xlim()[1], 120))
    finish(exp, fig, "latency_cdf.png")

    # retry_count distribution per mode
    fig, ax = plt.subplots(figsize=(6, 3.6))
    width = 0.8 / len(prefixes)
    for i, (p, l) in enumerate(zip(prefixes, labels)):
        counts = {}
        for df in cond.get(p, []):
            for v in df["retry_count"].fillna(0):
                counts[int(v)] = counts.get(int(v), 0) + 1
        mx = max(counts) if counts else 0
        xs = list(range(mx + 1))
        ys = [counts.get(x, 0) for x in xs]
        ax.bar([x + i * width for x in xs], ys, width=width, label=l,
               color=MODE_COLORS.get(l, "#8b949e"))
    ax.set_xlabel("retry_count"); ax.set_ylabel("tasks")
    ax.legend()
    finish(exp, fig, "retry_distribution.png")

    # summary
    rows = []
    for p, l in zip(prefixes, labels):
        frames = cond.get(p, [])
        st = run_stats(frames, "total_latency")
        sr_vals = [success_rate(df) for df in frames]
        rows.append(dict(mode=l,
                         success_rate=(statistics.mean(sr_vals) if sr_vals else float("nan")),
                         p95_total=st["p95"].mean() if not st.empty else float("nan"),
                         retried_avg=statistics.mean(
                             float(df["retry_count"].gt(0).mean()) for df in frames) if frames else float("nan"),
                         dlq_avg=statistics.mean(
                             int(df["model_output"].astype(str).str.contains("Exhausted", na=False).sum())
                             for df in frames) if frames else 0))
    save_summary(exp, pd.DataFrame(rows))


# ---- exp2: scheduling strategies ----------------------------------------
def plot_exp2():
    exp = "2"
    prefixes = ["exp2_fifo", "exp2_priority", "exp2_setf"]
    cond = load_condition(exp, prefixes)
    if not cond:
        print("  exp2: no data, skipping"); return
    labels = [p.replace("exp2_", "") for p in prefixes]

    # waiting_time boxplot (core chart)
    fig, ax = plt.subplots(figsize=(6, 4))
    data = []
    for p in prefixes:
        vals = []
        for df in cond.get(p, []):
            vals += pd.to_numeric(df["waiting_time"], errors="coerce").dropna().tolist()
        data.append(vals)
    bp = ax.boxplot(data, labels=labels, patch_artist=True, widths=0.5, showfliers=False)
    for patch, p in zip(bp["boxes"], prefixes):
        patch.set_facecolor(STRAT_COLORS.get(labels[prefixes.index(p)], "#8b949e"))
        patch.set_alpha(0.6)
    ax.set_yscale("log")   # waiting is heavily right-skewed (P50 ~50s vs P95 ~800s)
    ax.set_ylabel("Waiting time (s, log scale)")
    finish(exp, fig, "waiting_boxplot.png")

    # waiting_time CDF
    fig, ax = plt.subplots(figsize=(6, 4))
    for p, l in zip(prefixes, labels):
        vals = []
        for df in cond.get(p, []):
            vals += pd.to_numeric(df["waiting_time"], errors="coerce").dropna().tolist()
        vals = sorted(vals)
        if not vals:
            continue
        ax.plot(vals, [i / len(vals) for i in range(len(vals))],
                label=l, color=STRAT_COLORS.get(l, "#8b949e"), lw=1.8)
    ax.set_xlabel("Waiting time (s)"); ax.set_ylabel("CDF")
    ax.legend()
    finish(exp, fig, "waiting_cdf.png")

    # waiting by task type (grouped bar: strategy x type)
    fig, ax = plt.subplots(figsize=(8, 4))
    width = 0.8 / len(prefixes)
    for i, (p, l) in enumerate(zip(prefixes, labels)):
        means = []
        for t in TYPES:
            vals = []
            for df in cond.get(p, []):
                sub = df[df["task_type"] == t]
                vals += pd.to_numeric(sub["waiting_time"], errors="coerce").dropna().tolist()
            means.append(statistics.median(vals) if vals else float("nan"))
        ax.bar([x + i * width for x in range(len(TYPES))], means, width=width, label=l,
               color=STRAT_COLORS.get(l, "#8b949e"))
    ax.set_xticks(range(len(TYPES))); ax.set_xticklabels(TYPES)
    ax.set_ylabel("Median waiting time (s)"); ax.legend()
    finish(exp, fig, "waiting_by_type.png")

    # summary
    rows = []
    for p, l in zip(prefixes, labels):
        st = run_stats(cond.get(p, []), "waiting_time")
        if st.empty:
            continue
        rows.append(dict(strategy=l, p50=st["p50"].mean(), p95=st["p95"].mean(),
                         p99=st["p99"].mean(), mean=st["mean"].mean()))
    save_summary(exp, pd.DataFrame(rows))


# ---- exp2b: work stealing -----------------------------------------------
def plot_exp2b():
    exp = "2b"
    prefixes = ["exp2b_steal1", "exp2b_steal0"]
    cond = load_condition(exp, prefixes)
    if not cond:
        print("  exp2b: no data, skipping"); return
    labels = [p.replace("exp2b_", "").replace("1", "ON").replace("0", "OFF") for p in prefixes]

    fig, ax = plt.subplots(figsize=(6, 4))
    data = []
    for p in prefixes:
        vals = []
        for df in cond.get(p, []):
            vals += pd.to_numeric(df["waiting_time"], errors="coerce").dropna().tolist()
        data.append(vals)
    bp = ax.boxplot(data, labels=labels, patch_artist=True, widths=0.5, showfliers=False)
    for patch, i in zip(bp["boxes"], range(len(prefixes))):
        patch.set_facecolor("#2f81f7" if i == 0 else "#8b949e"); patch.set_alpha(0.6)
    ax.set_yscale("log")
    ax.set_ylabel("Waiting time (s, log scale)")
    finish(exp, fig, "steal_waiting_boxplot.png")

    rows = []
    for p, l in zip(prefixes, labels):
        st = run_stats(cond.get(p, []), "waiting_time")
        if st.empty:
            continue
        rows.append(dict(steal=l, p50=st["p50"].mean(), p95=st["p95"].mean(), mean=st["mean"].mean()))
    save_summary(exp, pd.DataFrame(rows))


# ---- exp3: resource specialisation --------------------------------------
def plot_exp3():
    exp = "3"
    prefixes = ["exp3_base", "exp3_specialist"]
    cond = load_condition(exp, prefixes)
    if not cond:
        print("  exp3: no data, skipping"); return
    labels = ["2 CPU generalists", "+ SIM/SUM specialists"]

    # cumulative completed tasks per worker over time. Runs are concatenated at
    # their within-run elapsed time (each run restarts at t=0) so the x-axis has
    # no gaps from runs executed on different days; plateaus then reflect a
    # worker spending time on a slow SUM execution rather than inter-run idle.
    for p, l in zip(prefixes, labels):
        frames = cond.get(p, [])
        if not frames:
            continue
        workers = sorted(set(w for f in frames for w in f["worker_name"].dropna()))
        series = {w: ([], []) for w in workers}   # elapsed, cumulative count
        offset = 0.0
        for f in frames:
            f = f.copy()
            f["ts"] = pd.to_datetime(f["timestamp"])
            f = f.sort_values("ts")
            run_elapsed = (f["ts"] - f["ts"].iloc[0]).dt.total_seconds()
            f["elapsed"] = run_elapsed + offset
            for w in workers:
                sub = f[f["worker_name"] == w]
                if sub.empty:
                    continue
                base = len(series[w][1])
                series[w][0].extend(sub["elapsed"].values)
                series[w][1].extend(range(base + 1, base + len(sub) + 1))
            offset += run_elapsed.max()
        fig, ax = plt.subplots(figsize=(7, 3.8))
        for w in workers:
            ax.plot(series[w][0], series[w][1], label=w, lw=1.6)
        ax.set_xlabel("Cumulative processing time (s)")
        ax.set_ylabel("Cumulative completed tasks")
        ax.set_title(l, fontsize=11)
        ax.legend(fontsize=8, ncol=2)
        finish(exp, fig, f"worker_throughput_{p.replace('exp3_', '')}.png")

    # SIM/SUM waiting: baseline vs specialist
    fig, ax = plt.subplots(figsize=(6, 3.8))
    width = 0.8 / 2
    for i, t in enumerate(["SIM", "SUM"]):
        for j, (p, l) in enumerate(zip(prefixes, labels)):
            vals = []
            for df in cond.get(p, []):
                sub = df[df["task_type"] == t]
                vals += pd.to_numeric(sub["waiting_time"], errors="coerce").dropna().tolist()
            m = statistics.median(vals) if vals else float("nan")
            ax.bar(i + j * width, m, width=width, label=(l if i == 0 else None),
                   color="#3fb950" if j == 0 else "#d29922")
    ax.set_xticks([x + width / 2 for x in range(len(["SIM", "SUM"]))])
    ax.set_xticklabels(["SIM", "SUM"]); ax.set_ylabel("Median waiting time (s)")
    ax.legend()
    finish(exp, fig, "specialist_waiting.png")

    rows = []
    for p, l in zip(prefixes, labels):
        frames = cond.get(p, [])
        if not frames:
            continue
        sim = []
        for df in frames:
            sub = df[df["task_type"] == "SIM"]
            sim += pd.to_numeric(sub["waiting_time"], errors="coerce").dropna().tolist()
        rows.append(dict(config=l, n_tasks=sum(len(f) for f in frames),
                         sim_median_wait=statistics.median(sim) if sim else float("nan")))
    save_summary(exp, pd.DataFrame(rows))


# ---- exp4: scalability ----------------------------------------------------
def plot_exp4():
    exp = "4"
    lambdas = [0.05, 0.1, 0.5, 1, 2, 4, 8, 16, 30]
    prefixes = [f"exp4_lambda{l}" for l in lambdas]
    cond = load_condition(exp, prefixes)
    if not cond:
        print("  exp4: no data, skipping"); return

    # lambda vs P50/P95/P99 waiting_time (queue wait = the saturation indicator;
    # NOTE: total_latency in the CSV equals execution_latency because the worker
    # sets first_attempt_time at pop, so it excludes queue waiting.)
    fig, ax = plt.subplots(figsize=(6.5, 4))
    for q, col, color in [("P50", "p50", "#2f81f7"), ("P95", "p95", "#d29922"), ("P99", "p99", "#f85149")]:
        ys = []
        for p in prefixes:
            st = run_stats(cond.get(p, []), "waiting_time")
            ys.append(st[col].mean() if not st.empty else float("nan"))
        ax.plot(lambdas, ys, marker="o", label=q, color=color, lw=1.8)
    ax.set_xlabel("Arrival rate λ (tasks/s)"); ax.set_ylabel("Waiting time (s)")
    ax.set_xscale("log"); ax.legend()
    finish(exp, fig, "lambda_latency.png")

    # lambda vs actual throughput
    fig, ax = plt.subplots(figsize=(6.5, 4))
    y_thr = []
    for p in prefixes:
        thr = []
        for df in cond.get(p, []):
            ts = pd.to_datetime(df["timestamp"])
            if len(ts) < 2:
                continue
            span = (ts.max() - ts.min()).total_seconds()
            thr.append(len(df) / span if span > 0 else float("nan"))
        y_thr.append(statistics.mean(thr) if thr else float("nan"))
    ax.plot(lambdas, y_thr, marker="s", color="#3fb950", lw=1.8)
    ax.set_xlabel("Arrival rate λ (tasks/s)"); ax.set_ylabel("Actual throughput (tasks/s)")
    ax.set_xscale("log")
    finish(exp, fig, "lambda_throughput.png")

    rows = []
    for l, p in zip(lambdas, prefixes):
        st = run_stats(cond.get(p, []), "waiting_time")
        if st.empty:
            continue
        rows.append(dict(lambda_=l, p50=st["p50"].mean(), p95=st["p95"].mean(),
                         p99=st["p99"].mean()))
    save_summary(exp, pd.DataFrame(rows))


# ---- exp5: adversarial robustness ----------------------------------------
def plot_exp5():
    exp = "5"
    densities = [10, 20, 30]
    prefixes = [f"exp5_jb{d}" for d in densities]
    cond = load_condition(exp, prefixes)
    if not cond:
        print("  exp5: no data, skipping"); return

    # detection rate per density (jailbreak tasks blocked / injected)
    fig, ax = plt.subplots(figsize=(6, 3.6))
    rows = []
    for d, p in zip(densities, prefixes):
        det = []
        for df in cond.get(p, []):
            jb = df[is_jailbreak(df)]
            det.append(len(jb))
        rows.append(dict(density_pct=d, blocked_avg=statistics.mean(det) if det else 0))
    ax.bar([str(d) + "%" for d in densities], [r["blocked_avg"] for r in rows],
           color="#2f81f7")
    ax.set_xlabel("Jailbreak injection density"); ax.set_ylabel("Blocked tasks (avg per run)")
    for r in rows:
        ax.text(str(r["density_pct"]) + "%", r["blocked_avg"] + 0.2, f"{r['blocked_avg']:.0f}",
                ha="center", fontsize=9)
    finish(exp, fig, "detection_count.png")

    # blocked jailbreak by task type
    fig, ax = plt.subplots(figsize=(6, 3.6))
    width = 0.8 / len(densities)
    for i, (d, p) in enumerate(zip(densities, prefixes)):
        counts = {t: 0 for t in TYPES}
        for df in cond.get(p, []):
            jb = df[is_jailbreak(df)]
            for t in jb["task_type"]:
                if t in counts:
                    counts[t] += 1
        ax.bar([x + i * width for x in range(len(TYPES))], [counts[t] for t in TYPES],
               width=width, label=f"{d}%")
    ax.set_xticks(range(len(TYPES))); ax.set_xticklabels(TYPES)
    ax.set_ylabel("Blocked jailbreak tasks"); ax.legend()
    finish(exp, fig, "jailbreak_by_type.png")

    save_summary(exp, pd.DataFrame(rows))


PLOTTERS = {"1": plot_exp1, "2": plot_exp2, "2b": plot_exp2b,
            "3": plot_exp3, "4": plot_exp4, "5": plot_exp5}


def main():
    ap = argparse.ArgumentParser(description="Generate experiment charts")
    ap.add_argument("--exp", choices=["1", "2", "2b", "3", "4", "5", "all"], default="all")
    ap.add_argument("--out", help="override output base dir (default: experiments/)")
    ap.add_argument("--max-run", type=int, default=5,
                    help="only use runs <= N (e.g. 3 = first three reps, for an interim report)")
    args = ap.parse_args()
    global DATA, MAX_RUN
    if args.out:
        DATA = args.out
    MAX_RUN = args.max_run
    os.makedirs(DATA, exist_ok=True)
    if args.exp == "all":
        for i in PLOTTERS:
            print(f"--- exp{i} ---")
            PLOTTERS[i]()
    else:
        PLOTTERS[args.exp]()
    print("DONE")


if __name__ == "__main__":
    main()
