# Architecture

## Overview

The lab models a three-region observability and alerting stack. Each region contains a self-contained slice:

| Per Region | Count |
|---|---|
| Workload containers | 3 |
| Local OpenTelemetry Collectors | 3 |
| vmagent | 1 |
| VictoriaMetrics | 1 |
| VictoriaLogs | 1 |
| vmalert | 1 |
| Alertmanager | 1 |

A single Grafana instance connects to all regional backends. **34 services total**.

## Diagram

```mermaid
flowchart LR
  subgraph APAC[APAC Region]
    subgraph APACW[Workloads]
      A1[apac-app-1]
      A2[apac-app-2]
      A3[apac-app-3]
    end

    subgraph APACC[Local OTEL Collectors]
      AC1[apac-otel-1]
      AC2[apac-otel-2]
      AC3[apac-otel-3]
    end

    AVM[apac-vmagent]
    AVMS[apac-victoriametrics]
    AVLS[apac-victorialogs]
    AVAL[apac-vmalert]
    AAM[apac-alertmanager]

    A1 --> AC1
    A2 --> AC2
    A3 --> AC3
    AC1 -->|OTLP metrics| AVM
    AC2 -->|OTLP metrics| AVM
    AC3 -->|OTLP metrics| AVM
    AC1 -->|OTLP logs| AVLS
    AC2 -->|OTLP logs| AVLS
    AC3 -->|OTLP logs| AVLS
    AVM -->|remote_write| AVMS
    AVAL -->|query rules| AVMS
    AVAL -->|active alerts| AAM
  end

  subgraph EU[Europe Region]
    subgraph EUW[Workloads]
      E1[eu-app-1]
      E2[eu-app-2]
      E3[eu-app-3]
    end

    subgraph EUC[Local OTEL Collectors]
      EC1[eu-otel-1]
      EC2[eu-otel-2]
      EC3[eu-otel-3]
    end

    EVM[eu-vmagent]
    EVMS[eu-victoriametrics]
    EVLS[eu-victorialogs]
    EVAL[eu-vmalert]
    EAM[eu-alertmanager]

    E1 --> EC1
    E2 --> EC2
    E3 --> EC3
    EC1 -->|OTLP metrics| EVM
    EC2 -->|OTLP metrics| EVM
    EC3 -->|OTLP metrics| EVM
    EC1 -->|OTLP logs| EVLS
    EC2 -->|OTLP logs| EVLS
    EC3 -->|OTLP logs| EVLS
    EVM -->|remote_write| EVMS
    EVAL -->|query rules| EVMS
    EVAL -->|active alerts| EAM
  end

  subgraph US[US Region]
    subgraph USW[Workloads]
      U1[us-app-1]
      U2[us-app-2]
      U3[us-app-3]
    end

    subgraph USC[Local OTEL Collectors]
      UC1[us-otel-1]
      UC2[us-otel-2]
      UC3[us-otel-3]
    end

    UVM[us-vmagent]
    UVMS[us-victoriametrics]
    UVLS[us-victorialogs]
    UVAL[us-vmalert]
    UAM[us-alertmanager]

    U1 --> UC1
    U2 --> UC2
    U3 --> UC3
    UC1 -->|OTLP metrics| UVM
    UC2 -->|OTLP metrics| UVM
    UC3 -->|OTLP metrics| UVM
    UC1 -->|OTLP logs| UVLS
    UC2 -->|OTLP logs| UVLS
    UC3 -->|OTLP logs| UVLS
    UVM -->|remote_write| UVMS
    UVAL -->|query rules| UVMS
    UVAL -->|active alerts| UAM
  end

  AVMS -->|Prometheus datasource| G[Grafana Global View]
  EVMS -->|Prometheus datasource| G
  UVMS -->|Prometheus datasource| G
  AVLS -->|VictoriaLogs datasource| G
  EVLS -->|VictoriaLogs datasource| G
  UVLS -->|VictoriaLogs datasource| G
```

## Signal Flow

| Source | Signal | Destination | Purpose |
|---|---|---|---|
| Workload | OTLP metrics | Local collector | Emit alert-driving metric |
| Workload | OTLP logs | Local collector | Emit rich context log |
| Local collector | Metrics | Regional vmagent | Regional metric ingress |
| Local collector | Logs | Regional VictoriaLogs | Regional log ingestion |
| vmagent | Prometheus remote_write | Regional VictoriaMetrics | Metrics persistence |
| vmalert | Query API | Regional VictoriaMetrics | Rule evaluation |
| vmalert | Alert notifications | Regional Alertmanager | Alert lifecycle management |
| Grafana | Datasource query | Regional VictoriaMetrics | Dashboards |
| Grafana | Datasource query | Regional VictoriaLogs | Logs and correlations |

## Event Model

A single logical event becomes two telemetry records:

### Metric Signal

- **Name**: `lab_alert_active`
- **Type**: Gauge
- **Values**: `1` (active/firing), `0` (cleared)
- **Labels**: `region`, `service`, `component`, `instance`, `alert_name`, `severity`, `alert_id`, `source`

### Log Signal

Rich context attributes including `alert_id`, `alert_name`, `state`, `region`, `service`, `component`, `severity`, `reason`, `message`, `correlation_id`, and `event_time`.

The `alert_id` field enables correlation between the metric and log signals in Grafana.

## Networking

All services share a single Docker bridge network (`lab-net`). Internal service-to-service traffic is not exposed to the host.

## Persistence

Named Docker volumes are provisioned for every stateful component (28 volumes total), ensuring state survives container restarts for meaningful failure testing.
