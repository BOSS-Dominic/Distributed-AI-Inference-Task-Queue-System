# Distributed AI Inference Task Queue System

A distributed task queue for heterogeneous AI model inference, with a web dashboard, hot-swappable scheduling policies, and a full fault-tolerance chain. Built as an MSc dissertation project at the University of Birmingham.

## Architecture

9 Docker containers orchestrated by Docker Compose:

- **Redis** — message broker and state store (queues, leases, config, heartbeats)
- **FastAPI Gateway** — stateless REST API for task submission
- **5 Workers** — 1 GPU worker + 2 CPU general workers + 2 specialist workers (SIM / SUM)
- **Workload Generator** — Poisson-arrival task injection with configurable failure rate
- **Streamlit Dashboard** — 7-page control plane for monitoring and policy hot-switching

## Key Features

- **4 scheduling policies, hot-swappable at runtime**: FIFO, Priority, SETF (Shortest-Expected-Time-First, data-driven), and Work-Stealing
- **Full fault-tolerance chain**: chaos fault injection → exponential backoff retry → dead-letter queue with replay → TTL worker leases
- **Atomic delayed-queue sweep** via Lua scripts (eliminates read-then-remove race condition)
- **Task registry as single source of truth**, with SQLite WAL-mode audit trail
- **6 controlled experiments × 5 repetitions = 115 runs** quantifying scheduling and recovery performance

## Tech Stack

Python · FastAPI · Redis · Docker Compose · SQLite · Streamlit · Pandas

## Quick Start

```bash
docker compose up -d
# Dashboard: http://localhost:9501
# API docs:  http://localhost:8000/docs
```

---

# 分布式 AI 推理任务队列系统

面向异构 AI 模型推理的分布式任务队列，包含 Web 监控面板、运行时热切换调度策略和完整容错链。伯明翰大学硕士毕业设计项目。

## 核心特性

- **4 种调度策略运行时热切换**：FIFO、优先级、SETF（数据驱动最短预期时间优先）、工作窃取
- **完整容错链**：混沌故障注入 → 指数退避重试 → 死信队列重放 → TTL 租约
- **Lua 脚本原子化延迟队列扫描**，消除读-删竞态
- **任务注册表单一事实源**，SQLite WAL 模式持久化审计日志
- **6 组对照实验 × 5 次重复 = 115 次运行**，量化验证调度与容错性能

## 技术栈

Python · FastAPI · Redis · Docker Compose · SQLite · Streamlit · Pandas
