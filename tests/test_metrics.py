"""Gateway /metrics endpoint contract (business metrics for Prometheus)."""

from fastapi.testclient import TestClient

from api.main import app


def test_metrics_endpoint_exposes_core_metrics():
    client = TestClient(app)
    resp = client.get("/metrics")
    assert resp.status_code == 200
    body = resp.text
    for name in (
        "gateway_tasks_submitted_total",
        "gateway_tasks_audited_total",
        "gateway_task_latency_seconds",
        "gateway_queue_depth",
    ):
        assert name in body, name
