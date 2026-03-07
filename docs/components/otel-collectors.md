# OpenTelemetry Collectors

## Role

Each workload has a dedicated local collector that acts as the **first durable hop** in the telemetry pipeline. This is the lab's key resilience mechanism.

## Why One Per Workload

Dedicating a collector per workload ensures:

- Independent buffering and retry for each workload
- No shared queue corruption across workloads
- Realistic failure-domain testing (stop one collector, others continue)

## Pipeline

```
Workload → [OTLP] → Local Collector → metrics → regional vmagent
                                     → logs   → regional VictoriaLogs
```

### Receivers

- `otlp` over gRPC (`:4317`) and HTTP (`:4318`)

### Processors

- `memory_limiter` — prevents OOM under backpressure (used on both pipelines)
- `batch/logs` — groups log records for efficient export (logs pipeline only; **not** used on the metrics pipeline to minimise alert latency)

### Exporters

- `otlphttp/metrics` — forwards metrics directly to regional VictoriaMetrics (sending queue disabled for low latency)
- `otlphttp/logs` — forwards logs to regional VictoriaLogs (disk-backed sending queue for durability)

### Extensions

- `file_storage` — persistent sending queue backed by disk
- `health_check` — health endpoint on `:13133`

## Persistent Queue

The `file_storage` extension provides a disk-backed WAL at `/var/lib/otelcol`. Each collector has its **own dedicated volume** so queues are never shared.

The persistent queue is used only for the **logs pipeline**. The metrics pipeline has `sending_queue: enabled: false` to minimise alert latency (see [Performance Tuning](../performance.md#3-otel-collector-disable-persistent-queue-for-metrics)).

If VictoriaLogs is temporarily unavailable, the collector queues logs to disk and replays them on recovery.

## Configuration

Each collector's config is bind-mounted from `config/<region>/otel-<n>/config.yaml`.

Example for `apac-otel-1`:

```yaml
extensions:
  file_storage:
    directory: /var/lib/otelcol
  health_check:
    endpoint: 0.0.0.0:13133

receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
      http:
        endpoint: 0.0.0.0:4318

processors:
  memory_limiter:
    check_interval: 1s
    limit_mib: 256
  batch/logs:
    timeout: 200ms
    send_batch_size: 256

exporters:
  otlphttp/metrics:
    endpoint: http://apac-victoriametrics:8428/opentelemetry
    sending_queue:
      enabled: false
    retry_on_failure:
      enabled: true
  otlphttp/logs:
    endpoint: http://apac-victorialogs:9428/insert/opentelemetry
    sending_queue:
      enabled: true
      storage: file_storage
    retry_on_failure:
      enabled: true

service:
  extensions: [file_storage, health_check]
  pipelines:
    metrics:
      receivers: [otlp]
      processors: [memory_limiter]
      exporters: [otlphttp/metrics]
    logs:
      receivers: [otlp]
      processors: [memory_limiter, batch/logs]
      exporters: [otlphttp/logs]
```

The only difference between collectors in the same region is the file name; collectors across regions differ in their exporter endpoints.
