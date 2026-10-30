# 简历 V2 最终版（阶段一完成，2026-10-30）

> 供求职工作区直接取用、打磨措辞。数字与事实以 `docs/PHASE1_SYNC_REPORT.md` 为准。
> **诚实线**：Ansible 仅"骨架 + CI 语法校验"（未真机执行）；K8s 未开始；云上是 CPU-only。

## 项目名称

分布式 AI 推理任务队列系统（MSc 毕业设计项目，74/100）

## 简历条目（中文，5 条）

**分布式 AI 推理任务队列系统** | 独立开发 | Python · FastAPI · Redis · Docker Compose · SQLite · Prometheus · Grafana · Elasticsearch · Filebeat · Kibana · GitHub Actions · Ansible

- **异构调度平台**：9 容器分布式架构（网关 + Redis + GPU/CPU 异构 worker + 负载生成器 + Streamlit 控制面板），实现 FIFO / Priority / SETF / 工作窃取 4 种可运行时热切换的调度策略；完整容错链（混沌注入 → 指数退避重试 → 死信队列重放 → TTL 租约），6 组对照实验 × 5 次重复（115 次运行）量化验证。
- **可观测性体系**：cAdvisor / redis_exporter / DCGM 采集容器、队列、GPU 指标，网关暴露 4 组业务指标（提交/完成计数、延迟直方图、队列深度）；Prometheus + Grafana + 8 条告警规则全部 **provisioning as code**；日志侧用 Filebeat → Elasticsearch → Kibana 搭建检索链路，可按 `task_type` + `status` 定位失败任务（已实测验证，截图见仓库 `docs/screenshots/`）。
- **CI/CD 流水线**：GitHub Actions 六阶段门禁——ruff 静态检查 → pytest 契约测试（14 个）→ compose 三套配置校验 + promtool 告警规则校验 + Ansible 语法校验 → 镜像构建 → GHCR 发布 → 手动部署；当前全绿。
- **云上部署（已完成并验证）**：阿里云 ECS（香港地域，免备案）CPU-only 部署——compose override 做环境差异化（GPU worker → CPU 化镜像、观测组件 profile 隔离、端口最小化）；Caddy 自动 HTTPS（Let's Encrypt）；安全组仅开 22/80/443；dashboard 加 HTTP basic auth 加固；线上面板与 Grafana 均在线可访问。
- **部署工程化**：Ansible（inventory + docker / firewall / app 三个 role）实现一条命令完成云上部署——安装 Docker、UFW 最小暴露、渲染 `.env`、构建 CPU worker、启动并做 HTTPS 健康检查；**已真机执行并验证幂等**（重复执行 `changed=0`），CI 另做 playbook 语法校验。

## 面试叙事（四句话）

1. "毕设是业务控制面（Streamlit 管任务/调度/故障注入），毕设后补齐生产级可观测性链路：cAdvisor/redis_exporter/DCGM 采数 → Prometheus 存储 → Grafana 面板+告警，全部代码化。"
2. "指标回答'哪里出了问题'，日志回答'为什么'——所以又加了 Filebeat → Elasticsearch → Kibana，按 task_type + status 直接定位失败任务。"
3. "CI 里用 promtool 校验告警规则、pytest 跑契约测试、compose 三套配置和 Ansible playbook 都做语法校验，全部作为 push 门禁。"
4. "云端用 compose override 做 CPU 化部署（安全组最小暴露、Caddy 自动 HTTPS、dashboard basic auth），GPU 加速路径在本地验证——真实资源约束下的工程权衡。"

## 数字速查（写简历前核对）

| 事实 | 数值 |
| --- | --- |
| 毕设得分 | 74/100（全年最高分科目） |
| 容器数 | 9（业务）/ 14（本地含可观测性栈）/ 12（云上） |
| 调度策略 | 4（FIFO / Priority / SETF / Work-Stealing） |
| 实验 | 6 组 × 5 次 = 115 次运行 |
| 契约测试 | 14 个（pytest） |
| 告警规则 | 8 条（CI 用 promtool 校验） |
| Prometheus 抓取目标 | 5 个（全绿） |
| 云上验证 | 100 任务注入、14.89 tasks/s、死信 0；IC 任务 CPU 推理 ~4s/个 |
| 云主机 | 阿里云 ECS 2核8G，香港，Ubuntu 22.04 |

## 阿里云部署要点（供面试展开）

- 平台：阿里云 ECS 2核8G（香港地域，免 ICP 备案）
- 部署：`docker compose -f docker-compose.yml -f docker-compose.cloud.yml up -d`
- 安全：安全组仅开 22（限源 IP）/80/443；dashboard 走 Caddy HTTP basic auth（因其挂载 docker.sock）；Redis/Prometheus/Grafana/网关全部容器内网
- HTTPS：Caddy 自动签发 Let's Encrypt 证书；Grafana 走 `/grafana` 子路径
- 镜像：`workers/Dockerfile.fast-cpu`（CPU torch + 4 模型预下载，支持 `HF_ENDPOINT` 加速）
- 线上地址：`https://metalblade.xyz`（demo / taskqueue2026）、`https://metalblade.xyz/grafana`

## 使用规则

- 本文档与 `docs/PHASE1_SYNC_REPORT.md`、`docs/PROJECT_HANDOFF.md` 同步维护
- **不能写**："K8s 部署"（未开始）；"云上 GPU 加速"（云上是 CPU）；Ansible 已真机执行 + 幂等验证，**可以写**
- **本文件所在仓库是公开的**：不要添加电话、邮箱、地址、证件号等联系方式；完整简历只在本地维护
