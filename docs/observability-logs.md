# Logs with Filebeat → Elasticsearch → Kibana (optional, local only)

Metrics answer *that* something is wrong; logs answer *why*. The Prometheus /
Grafana stack reports p95 latency and failure ratios, but it cannot tell you
which `task_type` failed or what the error was. The log pipeline closes that
gap: every gateway event is written as a JSON line, shipped by Filebeat into
Elasticsearch and searchable in Kibana by `task_type` and `status`.

```
gateway (JSON logs)  ->  ./logs/gateway.log  ->  Filebeat  ->  Elasticsearch  ->  Kibana
```

## Why not on the cloud host

The cloud demo runs on 2 vCPU / 8 GB. Elasticsearch (heap 512 MB), Kibana and
Filebeat add roughly 2.4 GB on top of the ~4.5 GB stack — too much for that
host. The ELK services therefore carry the compose profile `logs` and are not
started by a plain `up -d` (or by the cloud override). Run them locally only.

## Start

```bash
# Rebuild the gateway so it emits JSON logs, then start the log stack
docker compose build fastapi-gateway
docker compose --profile logs up -d

# Generate traffic
docker exec workload-generator python generator.py 100 20 25 25 25 25
```

Services: `elasticsearch` (localhost:9200), `kibana` (localhost:5601),
`filebeat` (reads `./logs/gateway.log`).

## Windows prerequisite

Elasticsearch 7.x needs a higher `vm.max_map_count`. On Docker Desktop:

```powershell
wsl -d docker-desktop sysctl -w vm.max_map_count=262144
```

If Elasticsearch exits with `max virtual memory areas vm.max_map_count [65530]
is too low`, this is the fix. The setting resets when Docker Desktop restarts.

Note: on Windows the bind-mounted `filebeat.yml` appears as world-writable
inside the container, and Filebeat refuses to load such a config
(`config file can only be writable by the owner`). The compose service passes
`--strict.perms=false` to disable that check; no action needed.

## Kibana

1. Open <http://localhost:5601> (no login — security is disabled for this local
   demo; never do this on a host exposed to the internet).
2. Stack Management → Index Patterns → Create index pattern → `gateway-logs*`
   → time field `@timestamp`.
3. Discover → filter by `task_type: SUM` and `status: FAILED` (or `EXHAUSTED`)
   to see the failing tasks and their error text.

## Acceptance checklist (only add it to the CV when all three pass)

- [x] Kibana returns failed tasks filtered by `task_type` + `status`
- [x] A deliberately induced failure is visible in Kibana within 30 seconds
- [x] A screenshot of the search result (results + timeline) is saved —
      `docs/screenshots/kibana-logs.png` (index pattern `gateway-logs*`,
      filters `task_type: SUM` + `status: FAILED`, 11 hits)

## Deliberately inducing a failure

Use the dashboard's fault-injection sliders (Settings page) to raise the
failure rate and set retries to 0, or from the command line:

```bash
docker exec redis-broker redis-cli set config:task_failure_rate 0.5
docker exec redis-broker redis-cli set config:max_retries 0
```

Then inject a small batch, wait for the dead-letter queue to receive entries,
and search Kibana for `status: EXHAUSTED`. Reset afterwards:

```bash
docker exec redis-broker redis-cli set config:task_failure_rate 0
```

## Notes

- Elasticsearch 7.17 is used deliberately: version 8 enables TLS and password
  authentication by default, which is unnecessary complexity for a local demo.
- The gateway writes to `/app/logs/gateway.log` (bind-mounted from `./logs`,
  gitignored) — Filebeat does not read `/var/lib/docker/containers`, avoiding
  root-owned host paths.
- Index is fixed (`gateway-logs`) rather than rolled over daily, so a single
  Kibana index pattern is enough.
