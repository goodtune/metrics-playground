# Workload Simulator

## Role

Each workload container represents an application that can raise and clear alerts. In the lab, this is a Python/Flask application rather than a real business service.

## API

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Health check |
| `GET` | `/alerts` | List active alerts |
| `POST` | `/raise` | Raise a new alert |
| `POST` | `/clear` | Clear an existing alert |

### POST /raise

Request body (all fields optional):

```json
{
  "alert_id": "custom-id",
  "alert_name": "HighLatency",
  "severity": "critical",
  "reason": "p99 latency exceeded 500ms",
  "message": "Database connection pool exhausted",
  "correlation_id": "incident-42"
}
```

If `alert_id` is omitted, a UUID is generated automatically.

Returns:

```json
{"status": "raised", "alert_id": "..."}
```

### POST /clear

Request body:

```json
{"alert_id": "<alert_id>"}
```

## What It Emits

Each raise or clear call produces **two signals**:

1. **Metric** — `lab_alert_active` gauge (`1` = raised, `0` = cleared) with labels for `region`, `service`, `component`, `instance`, `alert_name`, `severity`, `alert_id`, and `source`.
2. **Log** — structured OTLP log record with the full event context including `reason`, `message`, `correlation_id`, and `event_time`.

## Configuration

Environment variables:

| Variable | Description |
|---|---|
| `REGION` | Region identifier (apac, eu, us) |
| `SERVICE_NAME` | Service identity |
| `COMPONENT` | Component label |
| `INSTANCE` | Instance identity |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Local collector OTLP endpoint |

## Connectivity

- **Outbound only**: OTLP to its paired local collector
- No direct connection to VictoriaMetrics, Alertmanager, or any other backend

## Persistence

None. The workload is stateless. Losing it is itself a failure mode to test.
