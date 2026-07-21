"""
Streamlit dashboard — real-time monitoring and control for the task queue system. v1.4
"""

import os
import sqlite3
import subprocess
import time
import datetime

import pandas as pd
import plotly.graph_objects as go
import redis
import requests
import streamlit as st
from streamlit_option_menu import option_menu

from shared.task_registry import TASK_REGISTRY

st.set_page_config(
    layout="wide",
    page_title="AI Inference Task Queue",
    page_icon="⚡",
    initial_sidebar_state="expanded"
)

st.markdown("""
<style>
.stDeployButton { display: none !important; }

div[data-testid="metric-container"] {
    background-color: var(--secondary-background-color);
    border: 1px solid rgba(128, 128, 128, 0.2);
    padding: 1.5rem 1.5rem;
    border-radius: 8px;
    box-shadow: 0 4px 6px rgba(0,0,0,0.1);
}
div[data-testid="metric-container"] label {
    font-size: 14px !important; font-weight: 600 !important; opacity: 0.8;
}
div[data-testid="metric-container"] div[data-testid="stMetricValue"] {
    color: var(--primary-color) !important;
    font-size: 36px !important; font-weight: bold !important;
}

.top-status {
    text-align: right; font-size: 14px; margin-top: 15px; opacity: 0.8;
}
.top-status span { color: #3fb950; font-weight: bold; }

.bottom-bar {
    display: flex; justify-content: space-between; align-items: center;
    padding: 12px 20px; background-color: var(--secondary-background-color);
    border-radius: 8px; border: 1px solid rgba(128, 128, 128, 0.2);
    margin-top: 50px; margin-bottom: 10px; font-size: 13px;
    box-shadow: 0 4px 6px rgba(0,0,0,0.1);
}
.status-badge {
    background-color: #238636; color: white; padding: 4px 12px;
    border-radius: 12px; font-weight: bold;
}
.status-dot { color: #3fb950; }
.status-dot.error { color: #f85149; }
.status-dot.warn { color: #d29922; }

.section-desc { color: #8b949e; font-size: 13px; margin-top: -12px; margin-bottom: 16px; }
</style>
""", unsafe_allow_html=True)

REDIS_HOST = "redis-broker"
API_GATEWAY = "http://fastapi-gateway:8000"
DB_PATH = "/app/storage/metrics.db"

r = redis.Redis(
    host=REDIS_HOST, port=6379, db=0,
    decode_responses=True, socket_connect_timeout=3,
)

WORKER_TAGS = {
    "worker-fast-gpu":       {"label": " GPU (Generalist)",  "icon": "⚡", "color": "#a371f7", "gpu": True},
    "worker-slow-cpu-1":     {"label": " CPU Generalist 1",  "icon": "🐌", "color": "#2f81f7", "gpu": False},
    "worker-slow-cpu-2":     {"label": " CPU Generalist 2",  "icon": "🐌", "color": "#3fb950", "gpu": False},
    "worker-sim-specialist": {"label": " SIM Specialist",    "icon": "", "color": "#2f81f7", "gpu": False},
    "worker-sum-specialist": {"label": " SUM Specialist",    "icon": "", "color": "#d29922", "gpu": False},
}


# --- Session state ---
if "session_start" not in st.session_state:
    st.session_state.session_start = time.time()
if "logs_hidden" not in st.session_state:
    st.session_state.logs_hidden = False
if "confirm_delete" not in st.session_state:
    st.session_state.confirm_delete = False
if "settings_confirm" not in st.session_state:
    st.session_state.settings_confirm = False


@st.cache_data(ttl=1, show_spinner=False)
def get_queue_stats():
    try:
        return {
            "queue_heavy": r.llen("queue:heavy"),
            "queue_light": r.llen("queue:light"),
            "active_tasks": r.hlen("active:tasks"),
        }
    except Exception:
        return {"queue_heavy": 0, "queue_light": 0, "active_tasks": 0}

@st.cache_data(ttl=1, show_spinner=False)
def get_worker_heartbeats():
    status = {name: False for name in WORKER_TAGS}
    try:
        keys = r.keys("worker:*:heartbeat")
        for key in keys:
            for name in WORKER_TAGS:
                if name in key:
                    status[name] = True
    except Exception:
        pass
    return status

@st.cache_data(ttl=1, show_spinner=False)
def get_work_stealing_status():
    try:
        val = r.get("config:work_stealing")
        return str(val) != "0"
    except Exception:
        return True

TASK_TYPES = list(TASK_REGISTRY.keys())
TASK_COLORS = {t: TASK_REGISTRY[t]["color"] for t in TASK_TYPES}
TASK_NAMES = {t: TASK_REGISTRY[t]["name"] for t in TASK_TYPES}

def set_work_stealing(enabled: bool):
    try:
        r.set("config:work_stealing", "1" if enabled else "0")
        get_work_stealing_status.clear()
        return True
    except Exception:
        return False



@st.cache_data(ttl=10, show_spinner=False)
def get_docker_stats():
    try:
        result = subprocess.run(
            ["docker", "stats", "--no-stream",
             "--format", "{{.Name}}\t{{.CPUPerc}}\t{{.MemPerc}}",
             "worker-fast-gpu", "worker-slow-cpu-1", "worker-slow-cpu-2",
             "worker-sim-specialist", "worker-sum-specialist"],
            capture_output=True, text=True, timeout=3
        )
        stats = {}
        for line in result.stdout.strip().split("\n"):
            parts = line.split("\t")
            if len(parts) >= 3:
                name = parts[0]
                cpu_str = parts[1].replace("%", "").strip()
                mem_str = parts[2].replace("%", "").strip()
                try:
                    stats[name] = {"cpu": float(cpu_str), "mem": float(mem_str)}
                except ValueError:
                    stats[name] = {"cpu": 0.0, "mem": 0.0}
        return stats
    except Exception:
        return {}

@st.cache_data(ttl=10, show_spinner=False)
def get_gpu_utilization():
    try:
        result = subprocess.run(
            ["docker", "exec", "worker-fast-gpu",
             "nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3
        )
        if result.returncode == 0 and result.stdout.strip():
            parts = result.stdout.strip().split(",")
            return {
                "gpu_util": float(parts[0].strip()) if len(parts) > 0 else 0.0,
                "mem_used": float(parts[1].strip()) if len(parts) > 1 else 0.0,
                "mem_total": float(parts[2].strip()) if len(parts) > 2 else 0.0,
                "temp": float(parts[3].strip()) if len(parts) > 3 else 0.0,
            }
    except Exception:
        pass
    return None



@st.cache_data(ttl=2, show_spinner=False)
def get_sqlite_summary():
    try:
        uri = f"file:{DB_PATH}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        df_tp = pd.read_sql_query(
            "SELECT count(*) as cnt FROM task_history WHERE timestamp >= datetime('now', '-1 minute')", conn)
        throughput = round(df_tp["cnt"].iloc[0] / 60.0, 1)

        df_lat = pd.read_sql_query(
            "SELECT task_type, AVG(execution_latency) as avg_lat FROM task_history GROUP BY task_type", conn)
        lat_by_type = {}
        for _, row in df_lat.iterrows():
            if pd.notnull(row["avg_lat"]):
                lat_by_type[row["task_type"]] = round(row["avg_lat"], 4)

        total = pd.read_sql_query("SELECT COUNT(*) FROM task_history", conn).iloc[0, 0]
        success = pd.read_sql_query(
            "SELECT COUNT(*) FROM task_history WHERE final_status='SUCCESS'", conn).iloc[0, 0]
        failed = pd.read_sql_query(
            "SELECT COUNT(*) FROM task_history WHERE final_status='FAILED'", conn).iloc[0, 0]
        jailbreak = pd.read_sql_query(
            "SELECT COUNT(*) FROM task_history WHERE model_output LIKE '%JailbreakBlocked%'", conn).iloc[0, 0]
        avg_overall = pd.read_sql_query("SELECT AVG(execution_latency) FROM task_history", conn).iloc[0, 0]
        avg_overall = round(avg_overall, 2) if pd.notnull(avg_overall) else 0.0
        success_rate = round((success / total * 100), 1) if total > 0 else 0.0

        conn.close()
        return {
            "throughput": throughput, "lat_by_type": lat_by_type,
            "db_ok": True, "total": total, "success": success, "failed": failed,
            "success_rate": success_rate, "avg_latency": avg_overall, "jailbreak": jailbreak,
        }
    except Exception:
        return {
            "throughput": 0.0, "lat_by_type": {},
            "db_ok": False, "total": 0, "success": 0, "failed": 0,
            "success_rate": 0.0, "avg_latency": 0.0, "jailbreak": 0,
        }

@st.cache_data(ttl=2, show_spinner=False)
def get_sqlite_history(limit=None):
    try:
        uri = f"file:{DB_PATH}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        query = "SELECT task_id, task_type, worker_name, execution_latency, final_status, model_output, input_summary, timestamp FROM task_history ORDER BY timestamp DESC"
        if limit:
            query += f" LIMIT {limit}"
        df = pd.read_sql_query(query, conn)
        conn.close()
        if not df.empty:
            df.index = range(1, len(df) + 1)
        return df
    except Exception:
        return pd.DataFrame()

@st.cache_data(ttl=2, show_spinner=False)
def get_failed_tasks():
    try:
        uri = f"file:{DB_PATH}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        df = pd.read_sql_query(
            "SELECT task_id, task_type, worker_name, model_output, timestamp FROM task_history WHERE final_status='FAILED' ORDER BY timestamp DESC",
            conn)
        conn.close()
        if not df.empty:
            df.index = range(1, len(df) + 1)
        return df
    except Exception:
        return pd.DataFrame()

@st.cache_data(ttl=2, show_spinner=False)
def get_jailbreak_tasks():
    try:
        uri = f"file:{DB_PATH}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        df = pd.read_sql_query(
            "SELECT task_id, worker_name, model_output, input_summary, timestamp FROM task_history WHERE model_output LIKE '%JailbreakBlocked%' ORDER BY timestamp DESC",
            conn)
        conn.close()
        if not df.empty:
            df.index = range(1, len(df) + 1)
        return df
    except Exception:
        return pd.DataFrame()

@st.cache_data(ttl=2, show_spinner=False)
def get_worker_task_distribution():
    try:
        uri = f"file:{DB_PATH}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        df = pd.read_sql_query(
            "SELECT worker_name, task_type, COUNT(*) as cnt FROM task_history GROUP BY worker_name, task_type",
            conn)
        conn.close()
        return df
    except Exception:
        return pd.DataFrame()

@st.cache_data(ttl=2, show_spinner=False)
def get_latency_time_series():
    try:
        uri = f"file:{DB_PATH}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        df = pd.read_sql_query(
            "SELECT timestamp, execution_latency, task_type, final_status FROM task_history ORDER BY timestamp DESC LIMIT 200",
            conn)
        conn.close()
        return df
    except Exception:
        return pd.DataFrame()

@st.cache_data(ttl=2, show_spinner=False)
def get_all_tasks_asc():
    try:
        uri = f"file:{DB_PATH}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        df = pd.read_sql_query(
            "SELECT timestamp, execution_latency, task_type FROM task_history ORDER BY timestamp ASC",
            conn)
        conn.close()
        return df
    except Exception:
        return pd.DataFrame()

@st.cache_data(ttl=2, show_spinner=False)
def get_total_execution_by_type():
    try:
        uri = f"file:{DB_PATH}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        df = pd.read_sql_query(
            "SELECT task_type, SUM(execution_latency) as total_time, COUNT(*) as cnt "
            "FROM task_history GROUP BY task_type",
            conn)
        conn.close()
        return df
    except Exception:
        return pd.DataFrame()

def clear_all_caches():
    get_sqlite_summary.clear()
    get_sqlite_history.clear()
    get_worker_task_distribution.clear()
    get_latency_time_series.clear()
    get_failed_tasks.clear()
    get_jailbreak_tasks.clear()
    get_docker_stats.clear()
    get_gpu_utilization.clear()
    get_work_stealing_status.clear()

def delete_all_logs():
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute("DELETE FROM task_history")
        conn.commit()
        conn.close()
        clear_all_caches()
        return True
    except Exception:
        return False




def submit_text(task_type: str, text: str, text2: str = None):
    try:
        data = {"task_type": task_type, "text_data": text}
        if text2:
            data["text_data_2"] = text2
        resp = requests.post(
            f"{API_GATEWAY}/v1/inference/tasks",
            data=data, timeout=10)
        return resp.json(), resp.status_code
    except Exception as e:
        return {"error": str(e)}, 500

def submit_image(file_bytes, filename, mime_type):
    try:
        resp = requests.post(
            f"{API_GATEWAY}/v1/inference/tasks",
            data={"task_type": "IC"},
            files={"file": (filename, file_bytes, mime_type)}, timeout=15)
        return resp.json(), resp.status_code
    except Exception as e:
        return {"error": str(e)}, 500

def trigger_workload(total: int, rate: float,
                     ic: int = 0, sa: int = 0, sim: int = 0, sum_: int = 0):
    try:
        cmd = ["docker", "exec", "workload-generator", "python", "generator.py",
               str(total), str(rate), str(ic), str(sa), str(sim), str(sum_)]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        clear_all_caches()
        return result.stdout, result.stderr
    except Exception as e:
        return None, str(e)


# Sidebar navigation
with st.sidebar:
    st.markdown("### 💠 AI Task Queue\n<span style='color:#8b949e; font-size:13px;'>Distributed System Web Dashboard</span><br><br>", unsafe_allow_html=True)

    page = option_menu(
        menu_title=None,
        options=["Overview", "Tasks", "Workers", "Metrics", "Logs", "Alerts", "Settings"],
        icons=["house", "list-task", "people", "bar-chart-line", "file-text", "bell", "gear"],
        menu_icon="cast",
        default_index=0,
        styles={
            "container": {"padding": "0!important", "background-color": "transparent"},
            "icon": {"color": "var(--text-color)", "font-size": "18px", "opacity": "0.7"},
            "nav-link": {"font-size": "15px", "text-align": "left", "margin": "5px",
                         "color": "var(--text-color)", "--hover-color": "rgba(128,128,128,0.1)"},
            "nav-link-selected": {"background-color": "#1f6feb", "color": "#ffffff", "font-weight": "bold"},
        }
    )

fragment_decorator = getattr(st, "fragment", getattr(st, "experimental_fragment", lambda run_every: lambda f: f))


# 1. Overview
if page == "Overview":
    @fragment_decorator(run_every="2s")
    def render_overview():
        stats = get_queue_stats()
        heartbeats = get_worker_heartbeats()
        s = get_sqlite_summary()
        throughput = s["throughput"]
        lat_by_type = s["lat_by_type"]
        db_ok = s["db_ok"]
        total, success, failed = s["total"], s["success"], s["failed"]
        success_rate, avg_lat, jailbreak = s["success_rate"], s["avg_latency"], s["jailbreak"]

        col_title, col_status = st.columns([2, 1])
        with col_title:
            st.title("System Overview")
        with col_status:
            st.markdown(f"<div class='top-status'>🔄 Auto-refresh: <span>ON</span> &nbsp;&nbsp;&nbsp; Last updated: {datetime.datetime.now().strftime('%H:%M:%S')}</div>", unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)

        k1, k2, k3, k4 = st.columns(4)
        total_pending = stats["queue_heavy"] + stats["queue_light"]
        active_workers = sum(1 for v in heartbeats.values() if v)
        total_workers = len(WORKER_TAGS)

        with k1:
            st.metric("Queue Depth", f"{total_pending:,}")
        with k2:
            st.metric("Active Workers", f"{active_workers}/{total_workers}")
        with k3:
            st.metric("Throughput", f"{throughput} tasks/s")
        with k4:
            st.metric("Success Rate", f"{success_rate}%" if total > 0 else "N/A")

        st.markdown("<br>", unsafe_allow_html=True)

        c1, c2, c3 = st.columns(3)

        with c1:
            with st.container(border=True):
                st.markdown("##### Queue Length (Redis)")
                heavy_label = f"Heavy ({'/'.join(TASK_REGISTRY[t]['name'][:4] for t in TASK_TYPES if TASK_REGISTRY[t]['queue']=='heavy')})"
                light_label = f"Light ({'/'.join(TASK_REGISTRY[t]['name'][:4] for t in TASK_TYPES if TASK_REGISTRY[t]['queue']=='light')})"
                fig1 = go.Figure(data=[
                    go.Bar(x=[heavy_label, light_label],
                           y=[stats["queue_heavy"], stats["queue_light"]],
                           marker_color=['#a371f7', '#2f81f7'],
                           text=[stats["queue_heavy"], stats["queue_light"]],
                           textposition='outside', textfont=dict(color='gray'))
                ])
                max_y = max(stats["queue_heavy"], stats["queue_light"], 1)
                fig1.update_layout(
                    margin=dict(l=0, r=0, t=10, b=0), height=260,
                    paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)',
                    xaxis=dict(color='gray'),
                    yaxis=dict(showgrid=True, gridcolor='rgba(128,128,128,0.2)', color='gray',
                               range=[0, max_y * 1.2], rangemode='nonnegative')
                )
                st.plotly_chart(fig1, use_container_width=True, key="overview_queue")

        with c2:
            with st.container(border=True):
                st.markdown("##### Task Outcomes")
                if total > 0:
                    other_failed = max(0, failed - jailbreak)
                    fig2 = go.Figure(data=[go.Pie(
                        labels=['SUCCESS', 'FAILED (other)', 'JAILBREAK BLOCKED'],
                        values=[success, other_failed, jailbreak],
                        hole=0.4, marker_colors=['#3fb950', '#f85149', '#d29922'],
                        textinfo='label+value', textfont=dict(color='white')
                    )])
                    fig2.update_layout(
                        margin=dict(l=0, r=0, t=10, b=0), height=260,
                        paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)',
                        legend=dict(font=dict(color='gray', size=10))
                    )
                else:
                    fig2 = go.Figure()
                    fig2.update_layout(margin=dict(l=0, r=0, t=10, b=0), height=260,
                                       paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)')
                    fig2.add_annotation(text="No tasks processed yet", showarrow=False,
                                        font=dict(color='gray', size=16))
                st.plotly_chart(fig2, use_container_width=True, key="overview_outcomes")

        with c3:
            with st.container(border=True):
                st.markdown("##### Tasks Over Time")
                ts_all_df = get_all_tasks_asc()
                if not ts_all_df.empty and len(ts_all_df) >= 2:
                    ts_all_df["done_dt"] = pd.to_datetime(ts_all_df["timestamp"])
                    ts_all_df["submit_dt"] = ts_all_df["done_dt"] - pd.to_timedelta(
                        ts_all_df["execution_latency"].fillna(0), unit="s")
                    t0 = ts_all_df["submit_dt"].iloc[0]
                    events = []
                    for _, row in ts_all_df.iterrows():
                        events.append({"t": (row["submit_dt"] - t0).total_seconds(), "type": row["task_type"], "delta": 1})
                        events.append({"t": (row["done_dt"] - t0).total_seconds(), "type": row["task_type"], "delta": -1})
                    events.sort(key=lambda e: e["t"])
                    backlogs = {t: [] for t in TASK_TYPES}
                    cur = {t: 0 for t in TASK_TYPES}
                    times = []
                    for e in events:
                        cur[e["type"]] += e["delta"]
                        times.append(e["t"])
                        for t in TASK_TYPES:
                            backlogs[t].append(max(0, cur[t]))
                    total_span = times[-1] - times[0] if times else 60
                    if total_span <= 0:
                        total_span = 60
                    if total_span < 60:
                        x_title = "Seconds"
                        div = 1
                    elif total_span < 3600:
                        x_title = "Minutes"
                        div = 60
                    else:
                        x_title = "Hours"
                        div = 3600
                    fig3 = go.Figure()
                    for t in TASK_TYPES:
                        c = TASK_COLORS[t]
                        fig3.add_trace(go.Scatter(
                            x=[tv / div for tv in times], y=backlogs[t],
                            mode='lines', name=f'{t} Backlog', line=dict(color=c, width=2),
                            fill='tozeroy', fillcolor=f'rgba({int(c[1:3],16)},{int(c[3:5],16)},{int(c[5:7],16)},0.15)'))
                    fig3.update_layout(
                        margin=dict(l=0, r=0, t=10, b=0), height=260,
                        paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)',
                        xaxis=dict(title=x_title, color='gray', showgrid=False),
                        yaxis=dict(title="Queue Backlog", color='gray',
                                   showgrid=True, gridcolor='rgba(128,128,128,0.2)',
                                   rangemode='nonnegative'),
                        legend=dict(font=dict(color='gray', size=10), orientation="h",
                                    yanchor="top", y=-0.2, xanchor="center", x=0.5),
                        hovermode='x unified',
                    )
                    st.plotly_chart(fig3, use_container_width=True, key="overview_timeline")
                else:
                    fig3 = go.Figure()
                    fig3.update_layout(margin=dict(l=0, r=0, t=10, b=0), height=260,
                                       paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)')
                    fig3.add_annotation(text="No tasks completed yet", showarrow=False,
                                        font=dict(color='gray', size=16))
                    st.plotly_chart(fig3, use_container_width=True, key="overview_timeline")

        # Bottom bar
        redis_ok = False
        try:
            redis_ok = r.ping()
        except Exception:
            pass

        elapsed = time.time() - st.session_state.session_start
        d = int(elapsed // 86400)
        h = int((elapsed % 86400) // 3600)
        m = int((elapsed % 3600) // 60)
        s = int(elapsed % 60)
        if d > 0:
            uptime_str = f"{d}d {h}h {m}m"
        elif h > 0:
            uptime_str = f"{h}h {m}m {s}s"
        elif m > 0:
            uptime_str = f"{m}m {s}s"
        else:
            uptime_str = f"{s}s"

        st.markdown(
            f"<div class='bottom-bar'>"
            f"<div><span class='status-badge'>✔ System Status &nbsp; HEALTHY</span></div>"
            f"<div>"
            f"Redis <span class=\"{'status-dot' if redis_ok else 'status-dot error'}\">{'● Connected' if redis_ok else '● Disconnected'}</span>"
            f"&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;"
            f"SQLite <span class=\"{'status-dot' if db_ok else 'status-dot error'}\">{'● Connected' if db_ok else '● Error'}</span>"
            f"&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;"
            f"Gateway <span class=\"{'status-dot' if redis_ok else 'status-dot error'}\">{'● Reachable' if redis_ok else '● Error'}</span>"
            f"</div>"
            f"<div>Session uptime &nbsp; {uptime_str}</div>"
            f"</div>",
            unsafe_allow_html=True,
        )

    render_overview()


# 2. Tasks
elif page == "Tasks":
    st.title("📋 Tasks Management")
    st.markdown("<hr style='border-color: #30363d;'>", unsafe_allow_html=True)

    tab_manual, tab_stress = st.tabs([" Manual Upload", " Stress Simulator"])

    with tab_manual:
        man_tabs = st.tabs([" IC Image", " SA Sentiment", " SIM Similarity", " SUM Summary"])

        with man_tabs[0]:
            with st.container(border=True):
                st.subheader("🖼️ Image Captioning (IC)")
                image_files = st.file_uploader(
                    "Select one or more images",
                    type=["jpg", "jpeg", "png", "webp", "bmp"],
                    accept_multiple_files=True, key="manual_ic"
                )
                if image_files:
                    st.caption(f"{len(image_files)} image(s) selected")
                if st.button("📤 Submit IC Task(s)", type="primary", disabled=not image_files):
                    count_ok, count_fail = 0, 0
                    progress = st.progress(0)
                    for i, f in enumerate(image_files):
                        with st.spinner(f"Submitting {f.name} ({i+1}/{len(image_files)})..."):
                            resp, code = submit_image(f.getvalue(), f.name, f.type or "image/jpeg")
                        if code == 202:
                            count_ok += 1
                        else:
                            count_fail += 1
                        progress.progress((i + 1) / len(image_files))
                    clear_all_caches()
                    st.success(f"Done: {count_ok} accepted, {count_fail} failed out of {len(image_files)}")

        with man_tabs[1]:
            with st.container(border=True):
                st.subheader("💬 Sentiment Analysis (SA)")
                sa_text = st.text_area(
                    "Enter prompt(s) - one per line",
                    value="The product arrived on time and works perfectly.\nI am extremely disappointed with the quality.\nThe customer service was very helpful.",
                    height=120, key="manual_sa",
                    help="Each non-empty line = one SA task."
                )
                lines = [ln.strip() for ln in sa_text.strip().split("\n") if ln.strip()]
                st.caption(f"{len(lines)} prompt(s) detected")
                if st.button("📤 Submit SA Task(s)", type="primary", disabled=len(lines) == 0):
                    count_ok, count_fail = 0, 0
                    progress = st.progress(0)
                    for i, line in enumerate(lines):
                        with st.spinner(f"Submitting {i+1}/{len(lines)}..."):
                            resp, code = submit_text("SA", line)
                        if code == 202:
                            count_ok += 1
                        else:
                            count_fail += 1
                        progress.progress((i + 1) / len(lines))
                        time.sleep(0.05)
                    clear_all_caches()
                    st.success(f"Done: {count_ok} accepted, {count_fail} failed out of {len(lines)}")

        with man_tabs[2]:
            with st.container(border=True):
                st.subheader("🔗 Semantic Similarity (SIM)")
                col_a, col_b = st.columns(2)
                with col_a:
                    sim_text1 = st.text_area("Sentence A", value="The weather is nice today.", height=80, key="sim_a")
                with col_b:
                    sim_text2 = st.text_area("Sentence B", value="It is a beautiful day outside.", height=80, key="sim_b")
                if st.button("📤 Submit SIM Task", type="primary"):
                    resp, code = submit_text("SIM", sim_text1, sim_text2)
                    clear_all_caches()
                    if code == 202:
                        st.success(f"Accepted: {resp.get('task_id', '')}")
                    else:
                        st.error(f"Failed: {resp}")

        with man_tabs[3]:
            with st.container(border=True):
                st.subheader("📝 Text Summarization (SUM)")
                sum_text = st.text_area(
                    "Enter a paragraph to summarize",
                    value="Artificial intelligence has transformed many industries including healthcare, finance, and education. Machine learning models can now perform tasks that once required human expertise.",
                    height=120, key="manual_sum",
                    help="Longer text = better summary. t5-small on CPU may take 5-8s."
                )
                if st.button("📤 Submit SUM Task", type="primary", disabled=not sum_text.strip()):
                    resp, code = submit_text("SUM", sum_text.strip())
                    clear_all_caches()
                    if code == 202:
                        st.success(f"Accepted: {resp.get('task_id', '')}")
                    else:
                        st.error(f"Failed: {resp}")

    with tab_stress:
        with st.container(border=True):
            st.subheader("🌊 Poisson-Arrival Stress Simulator")
            if "total_tasks" not in st.session_state:
                st.session_state.total_tasks = 30
            if "stress_ic" not in st.session_state:
                st.session_state.stress_ic = 8
            if "stress_sa" not in st.session_state:
                st.session_state.stress_sa = 8
            if "stress_sim" not in st.session_state:
                st.session_state.stress_sim = 7
            if "stress_sum" not in st.session_state:
                st.session_state.stress_sum = 7

            def on_total_change():
                remain = st.session_state.total_tasks - st.session_state.stress_ic - st.session_state.stress_sa - st.session_state.stress_sim
                if remain < 0:
                    st.session_state.stress_sum = 0
                else:
                    st.session_state.stress_sum = remain

            def on_type_change():
                pass

            total = st.slider("Total Tasks", 10, 500, step=10, key="total_tasks", on_change=on_total_change)

            c1, c2 = st.columns(2)
            with c1:
                ic_count = st.slider("IC (Image Captioning)", 0, total, key="stress_ic", on_change=on_total_change)
                sa_count = st.slider("SA (Sentiment)", 0, total, key="stress_sa", on_change=on_total_change)
            with c2:
                sim_count = st.slider("SIM (Similarity)", 0, total, key="stress_sim", on_change=on_total_change)
                sum_count = st.slider("SUM (Summarization)", 0, total, key="stress_sum", on_change=on_total_change)

            actual_total = ic_count + sa_count + sim_count + sum_count
            arate = st.slider("Arrival Rate λ (tasks/s)", 5.0, 100.0, 20.0, 5.0)
            jb_est = max(1, (sa_count + sim_count + sum_count) // 10)
            if actual_total > 0:
                st.caption(f"IC: {ic_count} | SA: {sa_count} | SIM: {sim_count} | SUM: {sum_count} | ~{jb_est} jailbreak | ~{actual_total/arate:.0f}s expected")
            else:
                st.caption("Set at least one task type count > 0")

            if st.button("🚀 Inject Poisson Workload", type="primary"):
                with st.spinner(f"Injecting {actual_total} tasks @ λ={arate}..."):
                    stdout, stderr = trigger_workload(actual_total, arate, ic_count, sa_count, sim_count, sum_count)
                if stdout:
                    st.success("Done!")
                    with st.expander("Generator Log", expanded=True):
                        st.code(stdout, language="text")
                if stderr:
                    with st.expander("Stderr"):
                        st.warning(stderr)


# 3. Workers
elif page == "Workers":
    st.title("👥 Worker Nodes Status")
    st.markdown("<hr style='border-color: #30363d;'>", unsafe_allow_html=True)

    @fragment_decorator(run_every="2s")
    def render_workers():
        heartbeats = get_worker_heartbeats()
        dist_df = get_worker_task_distribution()
        docker_stats = get_docker_stats()
        gpu_info = get_gpu_utilization()

        # Heartbeat cards
        hc = st.columns(len(WORKER_TAGS))
        for i, (name, info) in enumerate(WORKER_TAGS.items()):
            with hc[i]:
                online = heartbeats.get(name, False)
                border_color = "rgba(63, 185, 80, 0.5)" if online else "rgba(248, 81, 73, 0.5)"
                bg_color = "rgba(63, 185, 80, 0.05)" if online else "rgba(248, 81, 73, 0.05)"
                status_html = "🟢 ONLINE" if online else "🔴 OFFLINE"

                html_content = (
                    f"""<div style="border: 1px solid {border_color}; border-radius: 8px; padding: 15px; background-color: {bg_color}; margin-bottom: 10px; height: 160px; box-shadow: 0 4px 6px rgba(0,0,0,0.1);">\n"""
                    f"""    <h3 style="margin-top:0; font-size:20px; margin-bottom:8px;">{status_html}</h3>\n"""
                    f"""    <p style="margin-bottom:12px; line-height:1.2; font-size:14px;">{info['icon']} <b>{name}</b><br><i style="color:#8b949e">{info['label']}</i></p>\n"""
                )

                if info["gpu"]:
                    if gpu_info:
                        gpu_util = gpu_info["gpu_util"]
                        gpu_mem_pct = (gpu_info["mem_used"] / gpu_info["mem_total"] * 100) if gpu_info["mem_total"] > 0 else 0
                        gpu_util_color = "#f85149" if gpu_util > 85 else ("#d29922" if gpu_util > 60 else "#3fb950")
                        gpu_mem_color = "#f85149" if gpu_mem_pct > 85 else ("#d29922" if gpu_mem_pct > 60 else "#3fb950")
                        html_content += (
                            f"""<div style="font-size:13px; line-height:1.4;">\n"""
                            f"""🎮 GPU Util: <span style='color:{gpu_util_color};font-weight:bold'>{gpu_util:.1f}%</span><br>\n"""
                            f"""└ VRAM: <span style='color:{gpu_mem_color};font-weight:bold'>{gpu_info['mem_used']:.0f}/{gpu_info['mem_total']:.0f} MB</span>\n"""
                            f"""</div>\n"""
                        )
                    else:
                        html_content += "<div style='font-size:13px; color:gray'>GPU data unavailable</div>"
                else:
                    ds = docker_stats.get(name, {})
                    cpu_val = ds.get("cpu", 0.0)
                    mem_val = ds.get("mem", 0.0)
                    cpu_color = "#f85149" if cpu_val > 80 else ("#d29922" if cpu_val > 50 else "#3fb950")
                    mem_color = "#f85149" if mem_val > 80 else ("#d29922" if mem_val > 50 else "#3fb950")
                    html_content += (
                        f"""<div style="font-size:13px; line-height:1.4;">\n"""
                        f"""🖥️ CPU Util: <span style='color:{cpu_color};font-weight:bold'>{cpu_val:.1f}%</span><br>\n"""
                        f"""└ MEM: <span style='color:{mem_color};font-weight:bold'>{mem_val:.1f}%</span>\n"""
                        f"""</div>\n"""
                    )

                html_content += "</div>"
                st.markdown(html_content, unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)

        st.subheader("Task Type Distribution per Worker")
        if not dist_df.empty:
            worker_names = dist_df["worker_name"].unique()
            wcols = st.columns(len(worker_names))
            for j, wname in enumerate(worker_names):
                wdf = dist_df[dist_df["worker_name"] == wname]
                with wcols[j]:
                    with st.container(border=True):
                        st.markdown(f"**{wname}**")
                        labels = wdf["task_type"].tolist()
                        values = wdf["cnt"].tolist()
                        total_w = sum(values)
                        fig_w = go.Figure(data=[go.Pie(
                            labels=labels, values=values, hole=0.5,
                            marker_colors=[TASK_COLORS.get(t, '#808080') for t in labels],
                            textinfo='percent', textfont=dict(color='white')
                        )])
                        fig_w.update_layout(
                            margin=dict(l=0, r=0, t=10, b=0), height=200,
                            paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)',
                            legend=dict(orientation="h", yanchor="bottom", y=-0.3, font=dict(color='gray'))
                        )
                        st.plotly_chart(fig_w, use_container_width=True, key=f"worker_pie_{wname}_{j}")
                        st.caption(f"Total: {total_w} tasks")
        else:
            st.info("No task data yet. Worker task distribution will appear here once tasks are processed.")

    render_workers()


# 4. Metrics
elif page == "Metrics":
    st.title("📊 System Metrics")
    st.markdown("<hr style='border-color: #30363d;'>", unsafe_allow_html=True)

    @fragment_decorator(run_every="5s")
    def render_metrics():
        s = get_sqlite_summary()
        _, lat_by_type = None, s["lat_by_type"]
        db_ok, total = s["db_ok"], s["total"]
        success, failed = s["success"], s["failed"]
        success_rate, avg_lat, jailbreak = s["success_rate"], s["avg_latency"], s["jailbreak"]

        sc = st.columns(6)
        sc[0].metric("Total Tasks", f"{total:,}")
        sc[1].metric("Success", f"{success:,}")
        sc[2].metric("Failed", f"{failed:,}")
        sc[3].metric("Jailbreak Blocked", f"{jailbreak:,}")
        sc[4].metric("Success Rate", f"{success_rate}%" if total > 0 else "N/A")
        sc[5].metric("Avg Latency", f"{avg_lat}s" if total > 0 else "N/A")

        st.markdown("<br>", unsafe_allow_html=True)

        ts_df = get_latency_time_series()
        ch1, ch2 = st.columns(2)

        with ch1:
            with st.container(border=True):
                st.markdown("##### Execution Latency (Last 200 Tasks)")
                if not ts_df.empty:
                    ts_df_rev = ts_df.iloc[::-1]
                    fig_ts = go.Figure()
                    for ttype in TASK_TYPES:
                        sub = ts_df_rev[ts_df_rev["task_type"] == ttype]
                        if not sub.empty:
                            fig_ts.add_trace(go.Scatter(
                                x=list(range(len(sub))), y=sub["execution_latency"],
                                mode='lines+markers', name=TASK_NAMES.get(ttype, ttype),
                                line=dict(color=TASK_COLORS[ttype], width=1.5), marker=dict(size=3)
                            ))
                    fig_ts.update_layout(
                        margin=dict(l=0, r=0, t=10, b=0), height=350,
                        paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)',
                        xaxis=dict(title="Task sequence", color='gray', showgrid=False),
                        yaxis=dict(title="Latency (s)", color='gray',
                                   showgrid=True, gridcolor='rgba(128,128,128,0.2)'),
                        legend=dict(font=dict(color='gray'))
                    )
                    st.plotly_chart(fig_ts, use_container_width=True, key="metrics_timeseries")
                else:
                    st.info("No latency data yet.")

        with ch2:
            with st.container(border=True):
                st.markdown("##### Avg Latency by Task Type")
                if lat_by_type:
                    fig_bar = go.Figure()
                    for ttype in TASK_TYPES:
                        val = lat_by_type.get(ttype, 0)
                        label = f"{val}s" if ttype in lat_by_type else "N/A"
                        fig_bar.add_trace(go.Bar(
                            name=TASK_NAMES.get(ttype, ttype), x=[ttype], y=[val],
                            marker_color=TASK_COLORS[ttype], width=0.4,
                            text=[label], textposition='outside', textfont=dict(color='gray')))
                    y_max = max(lat_by_type.values()) * 1.5 + 0.5 if lat_by_type else 5
                    fig_bar.update_layout(
                    margin=dict(l=0, r=0, t=10, b=0), height=350,
                    paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)', showlegend=False,
                    yaxis=dict(showgrid=True, gridcolor='rgba(128,128,128,0.2)', color='gray', range=[0, y_max]),
                    xaxis=dict(color='gray')
                )
                st.plotly_chart(fig_bar, use_container_width=True, key="metrics_latency_bar")

        st.markdown("<br>", unsafe_allow_html=True)
        ch3, ch4 = st.columns(2)
        with ch3:
            with st.container(border=True):
                st.markdown("##### Task Type Distribution")
                if total > 0 and not ts_df.empty:
                    type_counts = ts_df["task_type"].value_counts()
                    fig_type = go.Figure(data=[go.Pie(
                        labels=type_counts.index.tolist(),
                        values=type_counts.values.tolist(),
                        hole=0.5, marker_colors=[TASK_COLORS.get(t, '#808080') for t in type_counts.index.tolist()],
                        textinfo='label+percent', textfont=dict(color='white')
                    )])
                    fig_type.update_layout(
                        margin=dict(l=0, r=0, t=10, b=0), height=300,
                        paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)',
                        legend=dict(font=dict(color='gray'))
                    )
                    st.plotly_chart(fig_type, use_container_width=True, key="metrics_type_pie")
                else:
                    st.info("No data yet.")

        with ch4:
            with st.container(border=True):
                st.markdown("##### Total Execution Time by Task Type")
                exec_df = get_total_execution_by_type()
                if not exec_df.empty:
                    fig_bar2 = go.Figure(data=[
                        go.Bar(
                            x=exec_df["task_type"], y=exec_df["total_time"],
                            text=[f"{t:.2f}s<br>({c} tasks)" for t, c in zip(exec_df["total_time"], exec_df["cnt"])],
                            textposition='outside', textfont=dict(color='gray'),
                            marker_color=[TASK_COLORS.get(t, '#808080') for t in exec_df["task_type"]],
                        )
                    ])
                    fig_bar2.update_layout(
                        margin=dict(l=0, r=0, t=30, b=40), height=300,
                        paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)',
                        xaxis=dict(title="Task Type", color='gray'),
                        yaxis=dict(title="Total Execution Time (s)", color='gray',
                                   showgrid=True, gridcolor='rgba(128,128,128,0.2)'),
                    )
                    st.plotly_chart(fig_bar2, use_container_width=True, key="metrics_total_exec")
                else:
                    st.info("No execution data yet.")

    render_metrics()


# 5. Logs-
elif page == "Logs":
    st.title("📄 Recent Task History Logs")
    st.markdown("<hr style='border-color: #30363d;'>", unsafe_allow_html=True)

    btn_col1, btn_col2, btn_col3 = st.columns([1, 1, 2])
    with btn_col1:
        if st.button("🫥 Clear Display", use_container_width=True):
            st.session_state.logs_hidden = True
    with btn_col2:
        if "confirm_delete" not in st.session_state:
            st.session_state.confirm_delete = False
        if not st.session_state.confirm_delete:
            if st.button("🗑️ Delete All Logs", type="secondary", use_container_width=True):
                st.session_state.confirm_delete = True
                st.rerun()
        else:
            dc1, dc2 = st.columns(2)
            with dc1:
                if st.button("✅ Confirm Delete", type="primary", use_container_width=True):
                    ok = delete_all_logs()
                    if ok:
                        st.success("All logs deleted from database.")
                        st.session_state.logs_hidden = False
                    else:
                        st.error("Delete failed.")
                    st.session_state.confirm_delete = False
            with dc2:
                if st.button("❌ Cancel", use_container_width=True):
                    st.session_state.confirm_delete = False
                    st.rerun()

    st.markdown("<br>", unsafe_allow_html=True)

    @fragment_decorator(run_every="5s")
    def render_logs():
        if st.session_state.logs_hidden:
            st.info("Log display is hidden.")
            if st.button("🔓 Show Logs"):
                st.session_state.logs_hidden = False
                st.rerun()
            return
        df = get_sqlite_history()
        if not df.empty:
            def _color(val):
                if val == "SUCCESS":
                    return "background-color: rgba(63, 185, 80, 0.2); color: #3fb950"
                elif val == "FAILED":
                    return "background-color: rgba(248, 81, 73, 0.2); color: #f85149"
                return ""
            with st.container(border=True):
                st.dataframe(df.style.map(_color, subset=["final_status"]), use_container_width=True, height=600)
        else:
            st.info("No task history yet.")
    render_logs()


# 6. Alerts
elif page == "Alerts":
    st.title("🔔 Alerts & Notifications")
    st.markdown("<hr style='border-color: #30363d;'>", unsafe_allow_html=True)

    @fragment_decorator(run_every="3s")
    def render_alerts():
        heartbeats = get_worker_heartbeats()
        stats = get_queue_stats()
        docker_stats = get_docker_stats()
        gpu_info = get_gpu_utilization()
        s = get_sqlite_summary()
        total, success, failed = s["total"], s["success"], s["failed"]
        jailbreak = s["jailbreak"]
        failed_df = get_failed_tasks()
        jb_df = get_jailbreak_tasks()

        alerts = []

        # Heartbeat detection
        for name, online in heartbeats.items():
            if not online:
                info = WORKER_TAGS[name]
                alerts.append({
                    "level": "🔴 CRITICAL",
                    "source": "Heartbeat",
                    "message": f"{info['icon']} **{name}** ({info['label']}) is OFFLINE.",
                })

        # Resource overload detection
        for name, ds in docker_stats.items():
            cpu_val = ds.get("cpu", 0.0)
            mem_val = ds.get("mem", 0.0)
            info = WORKER_TAGS.get(name, {"icon": "🐳", "label": name})
            if cpu_val > 85:
                alerts.append({
                    "level": "🔴 CRITICAL",
                    "source": "Resources",
                    "message": f"{info['icon']} **{name}** CPU at **{cpu_val:.1f}%** - risk of crash!",
                })
            elif cpu_val > 60:
                alerts.append({
                    "level": "🟠 WARNING",
                    "source": "Resources",
                    "message": f"{info['icon']} **{name}** CPU at **{cpu_val:.1f}%** - elevated usage.",
                })
            if mem_val > 85:
                alerts.append({
                    "level": "🔴 CRITICAL",
                    "source": "Resources",
                    "message": f"{info['icon']} **{name}** Memory at **{mem_val:.1f}%** - possible OOM.",
                })
            elif mem_val > 60:
                alerts.append({
                    "level": "🟠 WARNING",
                    "source": "Resources",
                    "message": f"{info['icon']} **{name}** Memory at **{mem_val:.1f}%** - elevated usage.",
                })

        # GPU overload detection
        if gpu_info:
            if gpu_info["gpu_util"] > 90:
                alerts.append({
                    "level": "🔴 CRITICAL",
                    "source": "GPU",
                    "message": f"🎮 GPU utilization at **{gpu_info['gpu_util']:.1f}%** - near maximum.",
                })
            if gpu_info["temp"] > 80:
                alerts.append({
                    "level": "🟠 WARNING",
                    "source": "GPU",
                    "message": f"🌡️ GPU temperature at **{gpu_info['temp']:.0f}°C** - elevated.",
                })

        # High failure rate
        if total > 10:
            fail_rate = failed / total * 100
            if fail_rate > 20:
                other_failed = failed - jailbreak
                alerts.append({
                    "level": "🟠 WARNING",
                    "source": "Metrics",
                    "message": f"High failure rate: {failed}/{total} ({fail_rate:.1f}%). Non-jailbreak: {other_failed}.",
                })

        # Failure / jailbreak detection
        if failed > 0:
            other_failed = max(0, failed - jailbreak)
            lines = []
            if other_failed > 0:
                lines.append(f"{other_failed} task(s) failed (non-jailbreak).")
            if jailbreak > 0:
                lines.append(f"{jailbreak} jailbreak attempt(s) intercepted.")
            alerts.append({
                "level": "🟠 WARNING",
                "source": "Metrics & Security",
                "message": "<br>".join(lines) + f"<br>System is running but has errors - {failed} total failure(s).",
            })

        # Queue backlog
        q_depth = stats["queue_heavy"] + stats["queue_light"]
        if q_depth > 100:
            alerts.append({
                "level": "🟡 INFO",
                "source": "Queue",
                "message": f"Queue backlog: {q_depth} pending (Heavy: {stats['queue_heavy']}, Light: {stats['queue_light']}).",
            })

        if total == 0:
            alerts.append({
                "level": "ℹ️ INFO",
                "source": "System",
                "message": "No tasks processed. System is idle.",
            })

        if alerts:
            st.subheader("🚨 Active Alerts")
            for a in alerts:
                with st.container(border=True):
                    st.markdown(f"**{a['level']}**  |  *{a['source']}*")
                    st.markdown(a['message'])
        else:
            st.success("### ✅ All Systems Normal\nNo alerts. Workers healthy, resources stable, queues clear.")

        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown("---")
        st.subheader("❌ Failed Tasks")
        if not failed_df.empty:
            st.markdown(f"*{len(failed_df)} failed task(s) total*")
            def _color_fail(val):
                if "Jailbreak" in str(val):
                    return "background-color: rgba(210, 153, 34, 0.3); color: #d29922"
                return "background-color: rgba(248, 81, 73, 0.2); color: #f85149"
            st.dataframe(
                failed_df.style.map(_color_fail, subset=["model_output"]),
                use_container_width=True, height=250
            )
        else:
            st.info("No failed tasks.")

        st.markdown("---")
        st.subheader("🛡️ Jailbreak Interceptions")
        if not jb_df.empty:
            st.markdown(f"*{len(jb_df)} jailbreak attempt(s) blocked*")
            st.dataframe(jb_df, use_container_width=True, height=250)
        else:
            st.info("No jailbreak attempts detected.")

    render_alerts()


# 7. Settings-
elif page == "Settings":
    st.title("⚙️ Settings & Configuration")
    st.markdown("<hr style='border-color: #30363d;'>", unsafe_allow_html=True)

    with st.container(border=True):
        st.subheader("🔀 Work-Stealing Control")

        @fragment_decorator(run_every="2s")
        def render_stealing_control():
            stealing_enabled = get_work_stealing_status()

            sc1, sc2 = st.columns([2, 1])
            with sc1:
                if stealing_enabled:
                    st.markdown("**Status:** 🟢 **ENABLED** - GPU Worker will steal light-queue tasks when Heavy queue is empty.")
                else:
                    st.markdown("**Status:** 🔴 **DISABLED** - GPU Worker only processes Heavy queue. Light queue is CPU-only.")

            with sc2:
                if stealing_enabled:
                    if st.button("🔴 Disable Work-Stealing", type="secondary", use_container_width=True, key="disable_steal"):
                        set_work_stealing(False)
                        st.rerun()
                else:
                    if st.button("🟢 Enable Work-Stealing", type="primary", use_container_width=True, key="enable_steal"):
                        set_work_stealing(True)
                        st.rerun()


        render_stealing_control()

    st.markdown("<br>", unsafe_allow_html=True)

    with st.container(border=True):
        st.subheader("🔀 Scheduling Strategy")
        st.markdown("""
        | Component | Current Strategy | Description |
        |-----------|-----------------|-------------|
        | **Queue Discipline** | **FIFO** (BLPOP) | First-in-first-out via Redis blocking pop |
        | **Priority Scheduling** | None (planned 7/16) | Coming in Phase 2 |
        | **Retry Mechanism** | None (planned 7/23) | Exponential backoff retry coming in Phase 3 |
        | **Dead Letter Queue** | None (planned 7/23) | DLQ for tasks exceeding max retries coming in Phase 3 |
        """)

    st.markdown("<br>", unsafe_allow_html=True)

    with st.container(border=True):
        st.subheader("🔧 System Configuration")
        s1, s2, s3 = st.columns(3)
        with s1:
            st.markdown("**Redis Broker**\n`redis-broker:6379`")
        with s2:
            st.markdown("**API Gateway**\n`http://fastapi-gateway:8000`")
        with s3:
            st.markdown("**Database**\n`/app/storage/metrics.db` (WAL)")
        st.markdown("---")
        s4, s5 = st.columns(2)
        with s4:
            st.markdown(f"**Workers:** {len(WORKER_TAGS)} (1 GPU Generalist + 2 CPU Generalists + SIM Specialist + SUM Specialist)")
            st.markdown(f"**GPU Device:** RTX 5070 Ti (Passthrough)")
        with s5:
            st.markdown(f"**CPU Worker Limit:** 1.0 core each")
            st.markdown(f"**Jailbreak Detection:** Keyword pattern matching")

    st.markdown("<br>", unsafe_allow_html=True)

    with st.container(border=True):
        st.subheader("🗄️ Database Operations")
        sdb1, sdb2 = st.columns(2)
        with sdb1:
            s = get_sqlite_summary()
            db_ok, total = s["db_ok"], s["total"]
            st.metric("Total Records", f"{total:,}")
        with sdb2:
            try:
                fsize = os.path.getsize(DB_PATH)
                st.metric("DB File Size", f"{fsize / 1024 / 1024:.2f} MB")
            except Exception:
                st.metric("DB File Size", "N/A")

        if st.button("🗑️ Purge All Task History", type="secondary"):
            st.session_state.settings_confirm = True
            st.rerun()

        if st.session_state.get("settings_confirm", False):
            sc1, sc2 = st.columns(2)
            with sc1:
                if st.button("✅ Confirm Purge", type="primary"):
                    ok = delete_all_logs()
                    if ok:
                        st.success("All task history purged.")
                    else:
                        st.error("Purge failed.")
                    st.session_state.settings_confirm = False
            with sc2:
                if st.button("❌ Cancel Purge"):
                    st.session_state.settings_confirm = False
                    st.rerun()

    st.markdown("<br>", unsafe_allow_html=True)

    with st.container(border=True):
        st.subheader("🐳 Container Status")
        if st.button("🔄 Refresh Container Status"):
            try:
                result = subprocess.run(
                    ["docker", "ps", "--format", "table {{.Names}}\t{{.Status}}\t{{.Ports}}",
                     "--filter", "name=redis-broker|fastapi-gateway|worker-|workload-generator|streamlit-dashboard"],
                    capture_output=True, text=True, timeout=10
                )
                st.code(result.stdout, language="text")
            except Exception as e:
                st.error(f"Failed: {e}")
