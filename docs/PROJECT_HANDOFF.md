# 项目交接报告：毕设完成后的工程改造与后续任务

> 本文件是「毕设后改造」的**叙事与路线图**，供求职工作区对接进度使用。
> 工程事实的**权威来源**是仓库本身：`git log`、`README.md`、`docs/`、CI 配置。
> 原则：只读本文件，不要修改代码；简历中任何"已完成"表述必须与本文件一致。

## 一、背景与定位

- 毕设论文：*A Heterogeneous Scheduling and Fault-Tolerant Architecture*（伯明翰大学 MSc Advanced Computer Science），**得分 74/100**（Distinction 区间；学位等级 Merit，均分 64.333）
- 项目：9 容器分布式 AI 推理任务队列（FastAPI 网关 + Redis + 5 异构 worker + Poisson 负载生成器 + Streamlit 控制面板），4 种可热切换调度策略（FIFO/Priority/SETF/Work-Stealing）、完整容错链（混沌注入 → 退避重试 → 死信队列重放 → TTL 租约）、6 组对照实验 × 5 次重复 = 115 次运行
- 仓库：`https://github.com/BOSS-Dominic/Distributed-AI-Inference-Task-Queue-System`（公开）
- 分支结构：
  - `main` —— 论文最终版，标签 `v1.0-thesis`（保持不动）
  - `feat/observability` —— 毕设后演进版（可观测性 + CI/CD + 云上部署），CI 全绿

## 二、已完成改造（全部已提交、已验证）

### 1. 测试与可测试性（提交 9163c18）
- 抽取 `shared/retry_logic.py`（纯函数，可单测）
- 14 个 pytest 契约测试：任务注册表（4 类型/期望时间 0.3·0.8·3.4·11.6/队列映射）、退避公式、越狱词表、网关 `/metrics`
- `pyproject.toml` + `requirements-dev.txt` + 根 `conftest.py`

### 2. 网关业务指标（提交 f01a3fd）
- `api/main.py` 新增 `/metrics`（prometheus_client）：
  - `gateway_tasks_submitted_total{task_type,priority,delayed}`
  - `gateway_tasks_audited_total{task_type,status}`
  - `gateway_task_latency_seconds{task_type}`（直方图）
  - `gateway_queue_depth{queue}`（heavy/light/delayed/dead）
- `api/Dockerfile` 增加 prometheus-client 依赖

### 3. 可观测性栈（提交 dbed7be）
- compose 新增 5 容器：cAdvisor（容器指标）、redis_exporter（队列长度）、DCGM exporter 4.6.1-4.8.4（GPU 利用率/温度）、Prometheus（9090）、Grafana（3000）
- 8 条告警规则（`observability/prometheus/alert_rules.yml`），映射论文 Alerts 设计：容器丢失、CPU>85%、内存>8GiB、GPU>90%、GPU 温度>80°C、队列积压>50、死信非空、失败率>20%
- Grafana provisioning as code：datasource + 4 面板 dashboard JSON
- 镜像可经 `.env` 覆盖（`.env.example` 提供国内镜像源示例）

### 4. CI/CD（提交 2c6f1c1 / 31610c8 / 783dc12）
- GitHub Actions `.github/workflows/ci.yml`：`lint`(ruff) → `test`(pytest) → `validate`(compose + promtool check config/rules + dashboard JSON) → `build`(api/generator/dashboard 轻量镜像) → `publish`(GHCR，仅 main) → `deploy`(手动，自托管 runner 标签 `gpu-host`)
- 18GB GPU worker 镜像不进 CI（本地构建）
- README 双语（架构/分支/可观测性/CI 说明 + 实拍截图）

### 5. 可观测性实拍（提交 783dc12）
- `docs/screenshots/prometheus-targets.png`（5 个抓取目标全绿）
- `docs/screenshots/grafana-dashboard.png`（队列深度、GPU、吞吐+p95、失败率实时面板）

### 6. 云上部署（提交 c25d945 / 9b5e4b4）
- `docker-compose.cloud.yml`：`ports: !reset []` 取消全部主机端口；仅 Caddy 暴露 80/443；GPU worker 替换为 `worker-cpu-generalist`；dcgm/slow worker 用 profile 禁用；Grafana 走 `/grafana` 子路径
- `workers/Dockerfile.fast-cpu`：CPU torch + 4 模型 warm-up（含 BLIP），支持 `HF_ENDPOINT` 构建参数
- `Caddyfile`：自动 HTTPS（真实域名）+ `/grafana` 反向代理
- `docs/cloud-deploy.md`：安全组剧本（只开 22/80/443）、部署/验证/销毁步骤、成本表
- CI 新增云 compose 合并校验

### 7. 日志链路 ELK（本地，profile `logs`）
- 网关输出结构化 JSON（task_id / task_type / status / latency_s / retry_count）到 `./logs/gateway.log`
- `observability/filebeat/filebeat.yml` → Elasticsearch 7.17（固定 index `gateway-logs`）→ Kibana（按 task_type + status 过滤）
- compose 用 `profiles: ["logs"]` 隔离：默认 `up -d` 与云上都不会启动（云上 2核8G 内存放不下）
- 启动：`docker compose build fastapi-gateway && docker compose --profile logs up -d`
- 文档：`docs/observability-logs.md`（含 Windows `vm.max_map_count` 前置条件与验收清单）
- **已实测验证（2026-10-30）**：Kibana 按 `task_type: SUM` + `status: FAILED` 检索到 11 条失败任务，截图 `docs/screenshots/kibana-logs.png`；ES 索引 `gateway-logs` 正常写入

## 三、当前运行状态

### 本地 GPU 主机（开发/演示环境）

- 14 容器全部 Up；Prometheus 5 个 target 全绿（cadvisor/gateway/gpu/prometheus/redis）
- 已注入 100 任务（25×4 类），网关指标实时流动（audited：IC 25、SA 12、SIM 16、SUM 14；队列深度 light≈24 消化中）
- Grafana 面板实时刷新（含 GPU 利用率/温度）
- 实测内存：**全量 14 容器 ≈ 3.7 GiB**（云上 CPU 变体约 4.5 GiB → 4核8G 足够）
- 本地未提交（有意保留）：6 个 Dockerfile 的清华 pip 镜像源；`.env`（gitignored）

### 云上部署（阿里云 ECS，2026-10-29 完成，2026-10-30 HTTPS 验收通过）

- 实例：阿里云 ECS 2核8G（**香港地域，免 ICP 备案**），Ubuntu 22.04，公网 IP 47.238.247.136，域名 **metalblade.xyz**
- 12 容器全部运行且健康：CPU 化 `worker-cpu-generalist` + 2 个 specialist + 基础设施 + 可观测性栈；GPU 服务按 profile 禁用 ✓
- **业务链路验证通过**：注入 100 任务（14.89 tasks/s），队列正常消化、死信 0、IC 任务 CPU 推理 ~4s/个 SUCCESS
- 可观测性栈内部运行正常（Prometheus/Grafana）
- 端口暴露：**仅 22/80/443**（安全组 22 限源 IP）；Redis/网关/Prometheus/Grafana 全部仅容器内网
- **HTTPS 验收通过（2026-10-30）**：DNS 生效后 Caddy 自动签发 Let's Encrypt 证书；`https://metalblade.xyz` 返回 200（Streamlit 面板）、`/grafana` 302 → 登录页、`/grafana/login` 200 ✓
- 过程中修复：`.xyz` 注册当日实名审核导致 `client hold`（解析挂起）；`handle_path` 剥前缀导致 Grafana 子路径 301 自跳转 → 改为 `handle /grafana*` 透传

## 四、接下来任务路线

### 第 1 步：云上部署（✅ 已完成，待域名 DNS 同步收尾）
- Day 0（1-2 小时）：注册/学生认证（阿里云或腾讯云）、买 4核8G 学生机（**香港地域免 ICP 备案**，大陆机房需备案 1-3 周）、买域名 + DNS、安全组只开 22（限本机 IP）/80/443
- Day 1（2-4 小时）：
  ```bash
  git clone https://github.com/BOSS-Dominic/Distributed-AI-Inference-Task-Queue-System.git
  cd Distributed-AI-Inference-Task-Queue-System && git checkout feat/observability
  cp .env.example .env   # 填 DOMAIN / CLOUD_URL / GRAFANA_ADMIN_PASSWORD（必须改）
  docker compose -f docker-compose.yml -f docker-compose.cloud.yml build --build-arg HF_ENDPOINT=https://hf-mirror.com worker-cpu-generalist
  docker compose -f docker-compose.yml -f docker-compose.cloud.yml up -d
  ```
- 验证：手机流量访问 `https://<域名>`（Caddy HTTPS）；`docker compose ... ps` 确认 `worker-cpu-generalist` 在跑、GPU 服务未拉起；`https://<域名>/grafana` 面板自动加载；注入 100 任务看曲线
- Day 2：云上截图更新 README；产出简历 V2

### 第 2 步：K8s 迁移（可选，1 周冲刺，看 JD 反馈）
- ACK/TKE **CPU 节点**（GPU 节点池按小时计费，预算不划算）
- compose → manifests/Helm + Prometheus Operator + Ingress + **queue-depth 自定义指标 HPA**（SRE 亮点）
- **前提**：先等云主机跑通、拿到真实经验再写，不盲写

### 第 1.5 步：Ansible 部署工程化（可选，1-2 天，穿插在第 1 步后）
- `ansible/` 骨架已提交：inventory 模板 + deploy.yml（docker/firewall/app 三个 role）+ teardown + group_vars + CI 语法校验
- 部署命令：`ansible-playbook -i inventory.ini playbooks/deploy.yml`（幂等可重跑）
- 真实 inventory 与 vault 已 gitignore；CI 跑 `--syntax-check`
- **时点**：云主机跑通后 1-2 天内完成，不挤占投递/刷题时间

### 第 3 步：Terraform（砍掉或最后，收益边际）

## 五、纪律与红线（简历工作区必读）

1. **未完成的事不写**：云部署完成前不许写"已部署上云"；K8s 完成前不许写"K8s 生产部署"
2. **数字以本文件为准**：74/100、115 次运行、14 容器、14 个测试、8 条告警规则、5 个 target
3. **技术事实**：云上是 **CPU 推理演示**（BLIP 图像描述 ~1-3s/图、SUM ~15-25s/任务），GPU 加速在本地演示——这是诚实的工程权衡，面试可直接讲
4. **不要提"BLIP 出图"**——BLIP 是图像描述（image captioning，图像→文本）
5. 本地 pip 镜像改动与 `.env` 为本地专属，不提交

## 六、给求职工作区的对接要点

### 简历素材（已验证事实）
- 毕设 74/100（全年最高分科目）；9 容器分布式 AI 推理平台；4 种调度策略热切换；完整容错链
- 可观测性：cAdvisor + redis_exporter + DCGM → Prometheus → Grafana（4 面板 + 8 条告警规则，全部 provisioning as code）
- 业务指标埋点：网关 `/metrics`（提交/完成计数、延迟直方图、队列深度 gauge）
- CI/CD：GitHub Actions（ruff + 14 pytest + promtool 校验 + compose 校验 + 镜像构建 + GHCR 发布 + 自托管部署）
- 云上部署（V2 用）：Docker Compose override 环境差异化、Caddy 自动 HTTPS、安全组最小暴露面

### 面试叙事（三句话）
1. "毕设是业务控制面（Streamlit 管任务/调度/故障注入），毕设后补齐生产级可观测性链路：cAdvisor/redis_exporter/DCGM 采数 → Prometheus 存储 → Grafana 面板+告警，全部代码化"
2. "CI 里用 promtool 校验告警规则、pytest 跑契约测试、compose 校验，全部作为 push 门禁"
3. "云端用 compose override 做 CPU 化部署（安全组最小暴露、Caddy 自动 HTTPS），GPU 加速路径在本地验证——真实约束下的工程权衡"

### 对接方式
- 本文件路径：`docs/PROJECT_HANDOFF.md`（只读）
- 工程事实以 `README.md` + `git log --oneline feat/observability` + `docs/` 为准
- 本文件随分支提交，任何后续变更同步更新此文件
