# Getting Started

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and [Docker Compose](https://docs.docker.com/compose/install/) (v2+)

## Bring Up the Lab

```bash
docker compose up -d
```

### Recommended Start Order

Docker Compose handles dependency ordering via `depends_on`, but the logical order is:

1. VictoriaMetrics and VictoriaLogs (all regions)
2. vmagent (all regions)
3. Alertmanager (all regions)
4. vmalert (all regions)
5. Grafana
6. Local OTEL collectors
7. Workloads

## Exposed Ports

| Port | Service |
|---|---|
| 3000 | Grafana (admin/admin) |
| 8081–8083 | APAC workloads |
| 8084–8086 | EU workloads |
| 8087–8089 | US workloads |
| 9093 | APAC Alertmanager UI |
| 9094 | EU Alertmanager UI |
| 9095 | US Alertmanager UI |

## Raise an Alert

```bash
curl -X POST http://localhost:8081/raise \
  -H 'Content-Type: application/json' \
  -d '{
    "alert_name": "HighLatency",
    "severity": "critical",
    "reason": "p99 latency exceeded 500ms",
    "message": "Database connection pool exhausted"
  }'
```

## Clear an Alert

Use the `alert_id` returned by the raise call:

```bash
curl -X POST http://localhost:8081/clear \
  -H 'Content-Type: application/json' \
  -d '{"alert_id": "<alert_id>"}'
```

## List Active Alerts

```bash
curl http://localhost:8081/alerts
```

## Validation Sequence

1. Confirm the workload responds: `curl http://localhost:8081/health`
2. Raise an alert and verify it appears in VictoriaMetrics via Grafana at [http://localhost:3000](http://localhost:3000)
3. Check logs appear in VictoriaLogs via the Alert Context dashboard
4. Confirm vmalert fires the `LabAlertActive` rule
5. Confirm Alertmanager receives the alert at [http://localhost:9093](http://localhost:9093)
6. Clear the alert and confirm state transitions

## Tear Down

```bash
docker compose down
```

To also remove persistent volumes:

```bash
docker compose down -v
```
