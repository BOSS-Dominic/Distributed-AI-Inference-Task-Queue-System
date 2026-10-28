# Cloud deployment (CPU-only VM)

This document describes running the stack on a single CPU-only cloud VM with
HTTPS, using the `feat/observability` branch. The GPU worker is replaced by a
CPU generalist worker (`workers/Dockerfile.fast-cpu`), so the demo runs the full
task mix — image captioning, sentiment analysis, semantic similarity and
summarisation — on CPU. The GPU-accelerated path stays available locally.

## Prerequisites

- Ubuntu 22.04 VM, **4 vCPU / 8 GB RAM** minimum (the measured full stack uses
  ~4 GB including the OS).
- Docker Engine + Compose plugin (`docker compose version`).
- A domain (recommended) pointing at the VM's public IP — needed for a real
  TLS certificate from Let's Encrypt. Without one, Caddy serves plain HTTP.

### Security group / firewall (do not skip)

Open **only**:

| Port | Purpose |
| --- | --- |
| 22   | SSH — restrict the source to your own IP |
| 80   | HTTP → HTTPS redirect / ACME challenge |
| 443  | HTTPS (Caddy) |

**Block everything else.** The base compose file publishes Redis (6379), the
gateway (8000), Prometheus (9090), Grafana (3000), the dashboard (9501) and
cAdvisor (8081); the cloud override removes all of those host mappings. A Redis
instance exposed to the public internet is a well-known cryptocurrency-mining
target within hours of appearing online.

```bash
# Ubuntu UFW example
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
```

## Deploy

```bash
git clone https://github.com/BOSS-Dominic/Distributed-AI-Inference-Task-Queue-System.git
cd Distributed-AI-Inference-Task-Queue-System
git checkout feat/observability

cp .env.example .env
# edit .env:
#   DOMAIN=demo.example.com          (your domain)
#   CLOUD_URL=https://demo.example.com
#   GRAFANA_ADMIN_PASSWORD=...       (choose a password)

# First build downloads the four models at build time (~1.6 GB).
# On a slow network use the Hugging Face mirror:
docker compose -f docker-compose.yml -f docker-compose.cloud.yml \
  build --build-arg HF_ENDPOINT=https://hf-mirror.com worker-cpu-generalist

docker compose -f docker-compose.yml -f docker-compose.cloud.yml up -d
```

## Access

| URL | What |
| --- | --- |
| `https://<DOMAIN>` | Streamlit control dashboard (tasks, scheduling, fault injection) |
| `https://<DOMAIN>/grafana` | Grafana (admin / your password) — provisioning loads the dashboard automatically |

Prometheus, cAdvisor, redis_exporter, the gateway and the workers are **not**
published; they are reachable only inside the compose network.

## Verify

```bash
docker compose -f docker-compose.yml -f docker-compose.cloud.yml ps
```

Expected: `caddy`, `worker-cpu-generalist`, `worker-sim-specialist`,
`worker-sum-specialist`, `worker-slow-cpu-1/2` (extra profile off, so they are
absent), `redis-broker`, `fastapi-gateway`, `streamlit-dashboard`,
`prometheus`, `grafana`, `cadvisor`, `redis-exporter`. The GPU services
(`dcgm-exporter`, `worker-fast-gpu`) are disabled unless `--profile gpu` is
passed.

Smoke test from inside the VM:

```bash
docker exec streamlit-dashboard curl -fsS http://fastapi-gateway:8000/health
docker exec workload-generator python generator.py 100 20 25 25 25 25
# then open Grafana and watch queue depth / throughput / failure ratio
```

## Cost (student pricing, mainland China)

| Item | Approx. monthly |
| --- | --- |
| 4 vCPU / 8 GB lightweight VM (阿里云 / 腾讯云 student plans) | ¥30–100 |
| Domain | ~¥1/month |
| HTTPS | free (Let's Encrypt via Caddy) |

GPU cloud instances are billed by the hour (typically ¥3–15/h) and are not
needed for this demo — the GPU path is demonstrated locally.

## Teardown

```bash
docker compose -f docker-compose.yml -f docker-compose.cloud.yml down -v
```

Then release the VM / domain in the cloud console. Keep the security group
rules in place for future demos.

## Notes

- CPU inference is slower than the GPU host: image captioning takes ~1–3 s per
  image and summarisation ~15–25 s per task. The dashboard and metrics behave
  identically to the local GPU deployment.
- The dashboard mounts the Docker socket (used to trigger the workload
  generator). Acceptable for a demo host; do not run this stack on a shared or
  production host without replacing that mechanism.
