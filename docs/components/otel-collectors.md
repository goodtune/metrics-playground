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

- `memory_limiter` — prevents OOM under backpressure
- `batch` — groups telemetry for efficient export

### Exporters

- `otlphttp/metrics` — forwards metrics to regional vmagent
- `otlphttp/logs` — forwards logs to regional VictoriaLogs

### Extensions

- `file_storage` — persistent sending queue backed by disk
- `health_check` — health endpoint on `:13133`

## Persistent Queue

The `file_storage` extension provides a disk-backed WAL at `/var/lib/otelcol`. Each collector has its **own dedicated volume** so queues are never shared.

If the regional backend is temporarily unavailable, the collector queues telemetry to disk and replays it on recovery.

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
  batch:
    timeout: 1s
    send_batch_size: 1024

exporters:
  otlphttp/metrics:
    endpoint: http://apac-vmagent:8429
    sending_queue:
      enabled: true
      storage: file_storage
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
      processors: [memory_limiter, batch]
      exporters: [otlphttp/metrics]
    logs:
      receivers: [otlp]
      processors: [memory_limiter, batch]
      exporters: [otlphttp/logs]
```

The only difference between collectors in the same region is the file name; collectors across regions differ in their exporter endpoints.
