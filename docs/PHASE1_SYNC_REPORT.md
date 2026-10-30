# 阶段一同步报告：毕设后演进（可观测性 + CI/CD + 云上部署）

> 给求职工作区的自包含报告。**工程事实以仓库为准**（`README.md`、`git log`、`docs/`），
> 本文件是叙事与可用素材的汇总。**只读，不要改代码**；简历措辞以第五节的"诚实边界"为准。

## 一、一句话概括

毕设（74/100，MSc ACS 伯明翰）完成后，在 `feat/observability` 分支上把"能跑"的课程项目演进成
**带完整可观测性、CI/CD 门禁、云上 HTTPS 部署与部署自动化骨架**的工程化项目，并保持
`main` 为论文最终版（tag `v1.0-thesis`）。

## 二、阶段一已完成（全部有证据）

### 1. 测试与可测试性
- 抽取纯函数 `shared/retry_logic.py`（退避公式可单测）
- **14 个 pytest 契约测试**：任务注册表（4 类型/期望时间/队列映射）、退避公式、越狱词表、网关 `/metrics`
- `pyproject.toml` + `conftest.py` + CI 中运行

### 2. 网关业务指标（Prometheus instrumentation）
- `/metrics` 暴露 4 组指标：
  - `gateway_tasks_submitted_total{task_type,priority,delayed}`
  - `gateway_tasks_audited_total{task_type,status}`
  - `gateway_task_latency_seconds{task_type}`（直方图）
  - `gateway_queue_depth{queue}`（heavy/light/delayed/dead）

### 3. 可观测性 —— metrics 链路
- cAdvisor（容器）+ redis_exporter（队列长度）+ DCGM exporter 4.6.1-4.8.4（GPU）
- Prometheus 存储 + **8 条告警规则**（容器丢失/CPU>85%/内存>8GiB/GPU>90%/GPU 温度>80°C/队列积压>50/死信非空/失败率>20%）
- Grafana **provisioning as code**（datasource + 4 面板 dashboard JSON）
- 实测：Prometheus **5/5 targets 全绿**（截图 `docs/screenshots/prometheus-targets.png`）

### 4. 可观测性 —— logs 链路（ELK，本地）
- 网关输出结构化 JSON（`task_id / task_type / priority / status / latency_s / retry_count`）到 `./logs/gateway.log`
- Filebeat → Elasticsearch 7.17 → Kibana（固定 index `gateway-logs`）
- compose `profiles: ["logs"]` 隔离：云上不启动（2核8G 放不下）
- **实测验收通过**：Kibana 按 `task_type: SUM` + `status: FAILED` 检索到 11 条失败任务（截图 `docs/screenshots/kibana-logs.png`）

### 5. CI/CD（GitHub Actions，当前全绿）
- `lint`（ruff）→ `test`（pytest 14 个）→ `validate`（compose 基础/云 override/logs profile 三套 + promtool 配置与规则校验 + dashboard JSON 校验 + Ansible `--syntax-check`）→ `build`（api/generator/dashboard 镜像）→ `publish`（GHCR，仅 main）→ `deploy`（手动，自托管 runner `gpu-host`）
- 18GB GPU worker 镜像不进 CI（本地构建）

### 6. 云上部署（已完成并验证，2026-10-29/30）
- 阿里云 ECS **2核8G，香港地域（免 ICP 备案）**，Ubuntu 22.04，域名 **metalblade.xyz**
- **CPU-only 化**：`workers/Dockerfile.fast-cpu`（CPU torch + 4 模型预下载含 BLIP）、`worker-cpu-generalist` 替代 GPU worker
- `docker-compose.cloud.yml`：`ports: !reset []` 取消全部主机端口，**仅 Caddy 暴露 80/443**；GPU/多余 worker 用 profile 禁用；Grafana 走 `/grafana` 子路径
- **Caddy 自动 HTTPS**：Let's Encrypt 证书自动签发（DNS 生效后自动完成，无需人工）
- **安全加固**：安全组仅开 22（限本人 IP）/80/443；dashboard 加 **HTTP basic auth**（因其挂载 docker.sock）；Redis/网关/Prometheus 全走容器内网
- 12 容器全部运行；业务链路验证：注入 100 任务（14.89 tasks/s）、队列正常消化、死信 0、IC 任务 CPU 推理 ~4s/个
- 线上地址：`https://metalblade.xyz`（basic auth: demo / taskqueue2026）、`https://metalblade.xyz/grafana`（admin 密码私有）
- 云上实拍：`docs/screenshots/grafana-cloud.png`

### 7. 部署自动化（Ansible，已真机执行 + 幂等验证，2026-10-30）
- `ansible/`：inventory 模板 + `deploy.yml`（docker/firewall/app 三个 role）+ teardown + group_vars + CI `--syntax-check`
- 执行方式：Docker 容器作控制节点（`python:3.12-slim` + ansible-core），对云主机 `47.238.247.136` 跑 playbook
- **结果**：第一次 `ok=18 changed=2 failed=0`，第二次 `ok=18 changed=0 failed=0` → **幂等验证通过**
- 覆盖内容：装 Docker + Compose 插件、UFW（22/80/443，先放行后启用）、git 同步、`.env` 模板渲染、CPU worker 镜像构建、`up -d`、HTTPS 健康检查（接受 200/401，因为 dashboard 有 basic auth）
- 执行中修复的三个坑（面试素材）：
  1. Windows 绑定的目录是 world-writable → Ansible 忽略 `ansible.cfg` → 复制进容器非 world-writable 目录
  2. `community.general` 的 `yaml` stdout 回调已移除 → 改用 `callback_result_format = yaml`
  3. compose 显式 `container_name` + 不同 checkout 目录 → 项目名冲突 → `.env` 固定 `COMPOSE_PROJECT_NAME`

### 8. 文档与截图
- README 双语：架构/分支/可观测性/CI/CD/云上部署/ELK
- `docs/cloud-deploy.md`（安全组、部署、验证、成本、踩坑记录）
- `docs/observability-logs.md`（ELK 启动、Windows `vm.max_map_count`、Kibana 步骤、验收清单）
- 4 张截图：Prometheus targets / Grafana 本地 / Kibana / Grafana 云上

## 三、数字速查（写简历前核对）

| 事实 | 数值 |
|---|---|
| 毕设得分 | 74/100（全年最高分科目） |
| 容器 | 9（业务）/ 14（本地含可观测性）/ 12（云上） |
| 调度策略 | 4（FIFO / Priority / SETF / Work-Stealing） |
| 实验 | 6 组 × 5 次 = 115 次运行 |
| 契约测试 | 14 个 |
| 告警规则 | 8 条 |
| 抓取目标 | 5 个（全绿） |
| 云上任务注入 | 100 个，14.89 tasks/s，死信 0 |
| CPU 推理 | IC ~4s/个（GPU 0.3s，云上无 GPU） |
| 云主机 | 阿里云 ECS 2核8G，香港，Ubuntu 22.04 |

## 四、简历素材（已核实，可直接用）

**项目**：分布式 AI 推理任务队列系统（MSc 毕业设计，74/100）| Python · FastAPI · Redis · Docker Compose · SQLite · Prometheus · Grafana · Elasticsearch · Filebeat · Kibana · GitHub Actions · Ansible

1. **异构调度平台**：9 容器分布式架构 + 4 种可运行时热切换调度策略 + 完整容错链（混沌注入→退避重试→死信重放→TTL 租约），6 组对照实验 × 5 次重复（115 次运行）量化验证。
2. **可观测性体系**：cAdvisor / redis_exporter / DCGM 采集容器、队列、GPU 指标；网关暴露 4 组业务指标（提交/完成计数、延迟直方图、队列深度）；Prometheus + Grafana + 8 条告警规则全部 provisioning as code；日志侧 Filebeat → Elasticsearch → Kibana 按 `task_type` + `status` 定位失败任务（已实测，截图见仓库）。
3. **CI/CD 流水线**：GitHub Actions 六阶段门禁（ruff / pytest 14 个 / compose 三套校验 + promtool / 镜像构建 / GHCR 发布 / 手动部署），当前全绿。
4. **云上部署（已完成并验证）**：阿里云 ECS（香港，免备案）CPU-only 部署——compose override 环境差异化、Caddy 自动 HTTPS（Let's Encrypt）、安全组最小暴露面（仅 22/80/443）、dashboard basic auth 加固；线上面板与 Grafana 均可访问。
5. **部署工程化**：Ansible（inventory + docker/firewall/app 三个 role）实现一条命令完成云上部署——装 Docker、配置 UFW 最小暴露、渲染 `.env`、构建 CPU worker 镜像、启动并健康检查；**已真机执行并验证幂等**（重复执行 `changed=0`），CI 另做 playbook 语法校验。

## 五、诚实边界（简历不能越线）

| 内容 | 状态 | 简历写法 |
|---|---|---|
| 可观测性 metrics | ✅ 实测 | "已实现并验证" |
| ELK 日志链路 | ✅ 实测（本地） | "已实测验证" |
| CI/CD | ✅ 全绿 | "当前全绿" |
| 云上部署 + HTTPS | ✅ 实测 | "已完成并验证（线上可访问）" |
| Ansible | ✅ 已真机执行 + 幂等验证（`changed=0`） | "用 Ansible 一条命令完成云上幂等部署" |
| K8s 迁移 | ❌ 未开始 | 不写 |
| 云上 GPU | ❌ 无 | 写"CPU-only 部署，GPU 路径本地验证" |
| 云上推理性能 | ⚠️ CPU 数据（IC ~4s） | 别和 GPU 数据混用 |

## 六、后续路线（按优先级）

1. **投递 + 面试**（当务之急）：用第五节素材；面试展示线上地址 + 仓库截图
2. **Ansible 真机验证**（15-30 分钟，可选）：WSL 对云主机跑一次，通过后把第 5 条改为"已用 Ansible 一条命令部署"
3. **K8s 迁移**（仅当 JD 明确要求）：最小可演示 1-2 天（k3d + kube-prometheus-stack + Ingress + 截图）；完整版 3-5 天（含 queue-depth HPA）

## 七、对接方式

- 本报告：`docs/PHASE1_SYNC_REPORT.md`
- 简历草稿（最终版）：`docs/RESUME_V2.md`
- 路线图：`docs/PROJECT_HANDOFF.md`
- 工程事实：`README.md` + `git log --oneline feat/observability` + `docs/`
- GitHub raw（仓库公开）：
  `https://raw.githubusercontent.com/BOSS-Dominic/Distributed-AI-Inference-Task-Queue-System/feat/observability/docs/PHASE1_SYNC_REPORT.md`
