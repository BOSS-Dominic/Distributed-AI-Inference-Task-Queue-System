# 简历 V2 草稿（平台：阿里云）

> 供求职工作区直接取用、打磨措辞。所有数字与事实来自 `docs/PROJECT_HANDOFF.md`（权威）。
> **诚实线**：云上部署条目在阿里云实例验证跑通前，只能写"已实现部署方案"，
> 不得写"已部署/已上线/生产运行"。验证完成后把该条目升级为 V2.1（见文末）。

## 项目名称

分布式 AI 推理任务队列系统（MSc 毕业设计项目，74/100）

## 简历条目（中文）

**分布式 AI 推理任务队列系统** | 独立开发 | Python · FastAPI · Redis · Docker Compose · SQLite · Prometheus · Grafana · GitHub Actions

- **异构调度平台**：9 容器分布式架构（网关 + Redis + GPU/CPU 异构 worker + 负载生成器 + Streamlit 控制面板），实现 FIFO / Priority / SETF / 工作窃取 4 种可运行时热切换的调度策略；完整容错链（混沌注入 → 指数退避重试 → 死信队列重放 → TTL 租约），6 组对照实验 × 5 次重复（115 次运行）量化验证。
- **可观测性体系**：cAdvisor / redis_exporter / DCGM 采集容器、队列、GPU 指标，网关暴露业务指标（提交/完成计数、延迟直方图、队列深度），Prometheus 时序存储 + Grafana 面板与 8 条告警规则，全部 **provisioning as code** 可一键复现。
- **CI/CD 流水线**：GitHub Actions 实现 ruff 静态检查 → pytest 契约测试 → compose 配置校验 → promtool 告警规则校验 → 镜像构建 → GHCR 发布，全流程作为 push 门禁（当前全绿）。
- **部署工程化**（Ansible，可选，跑通后加入）：inventory + role（docker/firewall/app）实现一条命令幂等部署、UFW 最小暴露、环境变量与 vault 管理，CI 校验 playbook 语法。
- **云上部署方案**：设计并实现 CPU-only 云端部署（阿里云 ECS 方案）——CPU 化通用 worker 镜像、Docker Compose override 环境差异化、Caddy 自动 HTTPS、安全组最小暴露面（仅开 22/80/443），附带安全组与部署文档。

## 面试叙事（三句话）

1. "毕设是业务控制面（Streamlit 管任务/调度/故障注入），毕设后补齐生产级可观测性链路：cAdvisor/redis_exporter/DCGM 采数 → Prometheus 存储 → Grafana 面板+告警，全部代码化。"
2. "CI 里用 promtool 校验告警规则、pytest 跑契约测试、compose 校验，全部作为 push 门禁。"
3. "云端用 compose override 做 CPU 化部署（安全组最小暴露、Caddy 自动 HTTPS），GPU 加速路径在本地验证——真实约束下的工程权衡。"

## 数字速查（写简历前核对）

| 事实 | 数值 |
| --- | --- |
| 毕设得分 | 74/100（全年最高分科目） |
| 容器数 | 9（业务）/ 14（含可观测性栈） |
| 调度策略 | 4（FIFO / Priority / SETF / Work-Stealing） |
| 实验 | 6 组 × 5 次 = 115 次运行 |
| 契约测试 | 14 个（pytest） |
| 告警规则 | 8 条（Prometheus rule 文件，CI 用 promtool 校验） |
| Prometheus 抓取目标 | 5 个（全绿） |
| 部署方案 | 阿里云 ECS，CPU 推理（BLIP 图像描述 ~1-3s/图、SUM ~15-25s/任务），GPU 加速本地验证 |

## 阿里云部署要点（供面试展开）

- 平台：阿里云 ECS / 轻量应用服务器，4 核 8G（学生机），香港地域可免 ICP 备案
- 部署方式：`docker compose -f docker-compose.yml -f docker-compose.cloud.yml up -d`
- 网络与安全：安全组仅开放 22（限源 IP）/80/443；Redis、Prometheus、Grafana、网关全部走容器内网，不暴露公网
- HTTPS：Caddy 自动证书（Let's Encrypt）；Grafana 走 `/grafana` 子路径
- 镜像：`workers/Dockerfile.fast-cpu`（CPU torch + 4 模型预下载，支持 HF_ENDPOINT 加速）

## V2.1 升级（云上验证通过后，替换"云上部署方案"条目）

> **当前状态（2026-10-29）**：云端已部署并内部验证通过（阿里云 ECS 2核8G 香港、12 容器、100 任务注入、队列消化、死信 0、指标流动）；**公网 HTTPS 尚待 .xyz 域名注册局 NS 委派同步**（阿里云提示最迟次日）。HTTPS 验证通过后再启用下面的升级条目。
>
> 验证通过后，把第 4 条改为：
>
> - **云上部署**：在阿里云 ECS 完成 CPU-only 部署并验证（Caddy 自动 HTTPS、安全组最小暴露面、Grafana 面板在线可访问），配套 compose override 与部署文档；GPU 加速路径在本地验证。

## 使用规则

- 本文档与 `docs/PROJECT_HANDOFF.md` 同步维护；数字以 HANDOFF 为准
- 云上条目完成验证前，任何"已部署/已上线/生产运行"表述均不可使用
- **本文件所在仓库是公开的**：不要往任何提交进仓库的文件里添加电话、邮箱、地址、证件号等联系方式；完整简历只在本地维护
