# Performance Tuning

This page documents the alert round-trip latency characteristics of the lab, the optimisations applied to achieve them, and how to reproduce the measurements yourself.

## Round-Trip Latency

Round-trip latency is the wall-clock time from calling `/raise` on a workload to receiving the corresponding Alertmanager webhook back at the same workload. It captures every hop in the pipeline:

```
Workload /raise
  → SDK export (PeriodicExportingMetricReader)
    → OTel Collector forward
      → VictoriaMetrics ingest + index
        → vmalert rule evaluation
          → Alertmanager grouping + webhook
            → Workload /webhook received
```

### Baseline Results

A 10-minute load test across all 9 apps (3 regions × 3 apps) with mixed traffic patterns produced 189 measured round-trips:

| Metric | Value |
|--------|-------|
| Min | 1.4s |
| P50 | 3.4s |
| Average | 3.4s |
| P90 | 4.9s |
| P95 | 5.4s |
| P99 | 6.3s |
| Max | 6.3s |

91.5% of alerts were delivered within 5 seconds. No alert exceeded 6.3s.

### Latency Budget

Each pipeline stage contributes to the round-trip. With current tuning:

| Stage | Contribution | Why |
|-------|-------------|-----|
| SDK metric export | 0–1s | `PeriodicExportingMetricReader` exports every 1s; arrival timing within the interval is random |
| OTel Collector | ~0s | No batch processor on the metrics pipeline; immediate forward |
| VictoriaMetrics ingest | ~0s | `inmemoryDataFlushInterval=0s` makes data queryable immediately |
| vmalert evaluation | 0–1s | `evaluationInterval=1s`; depends on where in the cycle the data arrives |
| Alertmanager | ~1s | `group_wait=1s` before first notification for a new group |
| **Total theoretical** | **1–3s** | |

The gap between theoretical (1–3s) and observed (3–4s median) is explained by jitter alignment across stages. Each stage polls independently, so unlucky phase alignment adds up.

## Optimisations Applied

### 1. Pre-registered metric series

**Problem**: VictoriaMetrics takes 5–10 seconds to index a _new_ time series via its `indexdb` merge process. This is unavoidable regardless of ingestion protocol (OTLP or Prometheus remote_write).

**Discovery**: Raising the same alert twice showed the first raise took ~8s round-trip while re-raising the identical series took ~0.4s. The delay was entirely in new-series indexing, not data ingestion.

**Solution**: All 30 combinations of `(alert_name × severity)` are pre-registered at application startup. The `lab_alert_active` and `lab_alert_raised` gauges are initialised with value `0` by the workloads. The `lab_alert_closed` gauges are initialised with value `1` (always less than a real Unix timestamp) by the close-relay services. When an alert is raised or closed, the gauge updates an _existing_ series — no new indexing required.

```python
ALERT_NAMES = [
    "HighLatency", "DiskPressure", "MemoryExhausted", "CPUThrottle",
    "ConnectionPoolFull", "QueueBacklog", "CertExpiring", "ErrorRateSpike",
    "SlowQueries", "ReplicationLag",
]
SEVERITIES = ["critical", "warning", "info"]

# Pre-populate all (name × severity) with gauge value 0
for name in ALERT_NAMES:
    for severity in SEVERITIES:
        alert_gauge_values[key] = 0  # VM indexes these at startup
```

Alert IDs are deterministic via UUID5 so the same `(alert_name, severity)` always maps to the same series:

```python
def _make_alert_id(alert_name, severity):
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{SERVICE_NAME}.{alert_name}.{severity}"))
```

**Impact**: Round-trip dropped from 8–10s to 2–4s.

### 2. OTel Collector: remove batch processor for metrics

**Problem**: The default `batch` processor groups metrics for 1 second before forwarding, adding latency with no benefit when each workload is the sole producer.

**Solution**: The metrics pipeline uses only `memory_limiter` — no batch processor. Logs retain the batch processor since log latency is not on the critical path.

```yaml
# Metrics pipeline — no batch processor
pipelines:
  metrics:
    receivers: [otlp]
    processors: [memory_limiter]
    exporters: [otlphttp/metrics]
```

**Impact**: ~1s reduction.

### 3. OTel Collector: disable persistent queue for metrics

**Problem**: The `file_storage`-backed sending queue introduces ordering and backoff delays. While excellent for durability, it adds measurable latency on the metrics hot path.

**Solution**: Disable the sending queue for the metrics exporter. Keep it enabled for logs where durability matters more than speed.

```yaml
exporters:
  otlphttp/metrics:
    endpoint: http://<region>-victoriametrics:8428/opentelemetry
    sending_queue:
      enabled: false    # latency over durability for metrics
    retry_on_failure:
      enabled: true
```

!!! note
    This is a deliberate trade-off. With the queue disabled, metrics can be lost if VictoriaMetrics is temporarily unavailable. For the alerting use case, this is acceptable — a missed scrape simply delays detection by one evaluation interval. For production, evaluate whether durability or latency matters more for your metrics pipeline.

### 4. VictoriaMetrics: eliminate query delays

**Problem**: Two VictoriaMetrics defaults add seconds of query lag:

- `search.latencyOffset` (default `30s`) — delays queries to allow ingestion to settle
- `inmemoryDataFlushInterval` (default `5s`) — how often in-memory data is flushed to become queryable

**Solution**:

```yaml
command:
  - "-search.latencyOffset=0s"
  - "-inmemoryDataFlushInterval=0s"
```

!!! warning
    Setting `inmemoryDataFlushInterval=0s` increases disk I/O. In production, a value of `1s`–`2s` is a safer middle ground. `search.latencyOffset=0s` can cause vmalert to miss very recently ingested data on busy systems; the default of `30s` exists for good reason in high-throughput environments.

### 5. vmalert: fast evaluation cycle

**Problem**: The default 1-minute evaluation interval and 2-second evaluation delay are designed for production stability, not low-latency alerting.

**Solution**:

```yaml
command:
  - "-evaluationInterval=1s"
  - "-rule.evalDelay=0s"
```

The `EventAlertActive` rule uses `for: 0s` to fire immediately without a pending period:

```yaml
rules:
  - alert: EventAlertActive
    expr: |
      last_over_time(lab_alert_raised[24h])
        unless on(alert_name, service, severity)
      (
        last_over_time(lab_alert_closed[24h])
          >= on(alert_name, service, severity)
        last_over_time(lab_alert_raised[24h])
      )
    for: 0s
```

This range query is heavier than the original instant check (`lab_alert_active == 1`), so pre-registration of both `lab_alert_raised` and `lab_alert_closed` series is critical to avoid compounding query cost with new-series indexing delays.

### 6. Alertmanager: per-alert grouping

**Problem**: Alertmanager groups alerts to reduce notification volume. Without `alert_id` in `group_by`, multiple alerts with the same `(region, service, alert_name, severity)` are merged into one webhook, losing individual round-trip tracking.

**Solution**: Add `alert_id` to `group_by` and minimise wait times:

```yaml
route:
  group_by: ['region', 'service', 'alert_name', 'severity', 'alert_id']
  group_wait: 1s
  group_interval: 1s
  repeat_interval: 1m
```

## What Did Not Work

### Slot-pool approach

An earlier attempt pre-allocated "slot" series (`__slot_01`, `__slot_02`, ...) and relabelled them on raise. This failed because changing the `alert_name` label created a _new_ series from VictoriaMetrics' perspective, triggering the same 5–10s indexing delay. Label values are part of the series identity.

### `inmemoryDataFlushInterval` alone

Setting this to `200ms` improved flush latency but did not fix the core problem. New-series indexing happens in a separate `indexdb` merge process that is independent of the data flush interval.

### Prometheus remote_write vs OTLP

Both ingestion protocols exhibited the same 5–10s new-series delay. The bottleneck is in VictoriaMetrics' indexing, not the ingestion path. The lab uses OTLP throughout for simplicity.

## Running the Load Test

### Prerequisites

- The full lab stack running: `docker compose up -d --build`
- Python 3.10+ on the host (uses only stdlib — no additional packages required)
- Wait ~25 seconds after startup for pre-registered series to be indexed

### Quick single-alert test

Verify the pipeline is working with a single alert:

```bash
# Raise
curl -s -X POST http://localhost:8087/raise \
  -H 'Content-Type: application/json' \
  -d '{"alert_name": "HighLatency", "severity": "warning"}' | python3 -m json.tool

# Watch for round-trip (should appear within ~5s)
docker compose logs --since 10s --follow us-app-1 2>&1 | grep '\[round-trip\]'
```

### 10-minute load test

The load test script raises and clears alerts across all 9 apps with three traffic patterns:

- **Burst** — 3–9 simultaneous alerts across multiple regions
- **Sporadic** — 1–2 alerts on random apps (most common)
- **Steady** — one alert per region

```bash
python3 scripts/load-test.py
```

To run for a different duration (in seconds):

```bash
python3 scripts/load-test.py 300   # 5 minutes
```

The script clears all existing alerts before starting, then prints each raise/clear action as it happens. Press `Ctrl+C` to stop early.

### Collecting results

While the load test is running (or after it completes), extract round-trip latencies from the container logs:

```bash
# Raw latencies
docker compose logs --since 620s 2>&1 | grep '\[round-trip\]'

# Summary statistics
docker compose logs --since 620s 2>&1 \
  | grep '\[round-trip\]' \
  | sed 's/.*\[round-trip\] [A-Za-z]*: //' \
  | sed 's/s$//' \
  | python3 -c "
import sys
vals = sorted(float(l) for l in sys.stdin if l.strip())
n = len(vals)
print(f'Count: {n}')
print(f'Min:   {min(vals):.3f}s')
print(f'Avg:   {sum(vals)/n:.3f}s')
print(f'P50:   {vals[n//2]:.3f}s')
print(f'P90:   {vals[int(n*0.9)]:.3f}s')
print(f'P99:   {vals[int(n*0.99)]:.3f}s')
print(f'Max:   {max(vals):.3f}s')
under5 = sum(1 for v in vals if v <= 5)
print(f'<=5s:  {under5}/{n} ({100*under5/n:.1f}%)')
"
```

### Grafana dashboard

The round-trip latency is also available as the `lab_alert_roundtrip_seconds` metric in Grafana at [http://localhost:3000](http://localhost:3000). The Alert Pipeline dashboard includes a panel showing this metric across all regions.

### Clean run

For the most reproducible results, start from a clean state:

```bash
docker compose down -v
docker compose up -d --build
sleep 25   # wait for series pre-registration
python3 scripts/load-test.py
```
