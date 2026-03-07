# Failure Modes

The lab exists specifically so components can be stopped and restarted while observing behaviour.

## How to Simulate Failures

Stop a specific container:

```bash
docker compose stop <service-name>
```

Restart it:

```bash
docker compose start <service-name>
```

## Workload Stopped

```bash
docker compose stop apac-app-1
```

**Expected observations:**

- No new telemetry from that workload
- Existing active alert eventually resolves if the workload no longer refreshes the metric
- Historical metrics and logs remain visible in Grafana

## Local Collector Stopped

```bash
docker compose stop apac-otel-1
```

**Expected observations:**

- Workload fails to export telemetry (cannot reach collector)
- Data already queued in the collector's persistent volume survives
- On restart, queued data replays to backends
- Other workloads in the region are unaffected

## Regional vmagent Stopped

```bash
docker compose stop apac-vmagent
```

**Expected observations:**

- Local collectors continue queueing metrics (disk-backed)
- Logs continue flowing directly to VictoriaLogs
- Metric-based alert updates stall until vmagent returns
- On recovery, queued metrics replay

## Regional VictoriaMetrics Stopped

```bash
docker compose stop apac-victoriametrics
```

**Expected observations:**

- Local collectors and vmagent back up metrics
- vmalert query failures — no new alert evaluations
- Logs are unaffected
- On recovery, backlogged metrics ingest

## Regional VictoriaLogs Stopped

```bash
docker compose stop apac-victorialogs
```

**Expected observations:**

- Local collectors queue log traffic (disk-backed)
- Metric-driven alerts still function normally
- Rich context becomes delayed until log backend recovers

## Regional vmalert Stopped

```bash
docker compose stop apac-vmalert
```

**Expected observations:**

- Metrics continue ingesting normally
- No new alert state transitions sent to Alertmanager
- On restart, rule evaluation resumes from current metric state

## Regional Alertmanager Stopped

```bash
docker compose stop apac-alertmanager
```

**Expected observations:**

- vmalert cannot deliver alerts during outage
- Metric ingestion and rule evaluation continue
- After Alertmanager returns, active alerts re-sent on subsequent evaluations

## Grafana Stopped

```bash
docker compose stop grafana
```

**Expected observations:**

- All telemetry and alerting continue normally
- Only the viewing plane is lost
- On restart, all data is still available
