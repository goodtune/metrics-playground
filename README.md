# Metrics Playground

A local, Docker Compose-based lab for evaluating event-based alerting with OpenTelemetry, VictoriaMetrics, VictoriaLogs, vmalert, Alertmanager, and Grafana.

## Overview

This lab models a three-region (APAC, EU, US) observability stack with **34 services**. Each region has 3 workloads, 3 local OTEL collectors, a vmagent, VictoriaMetrics, VictoriaLogs, vmalert, and Alertmanager. A single Grafana instance provides the global view.

One event produces two signals:

- A **metric** (`lab_alert_active` gauge) that drives the alerting decision
- A **log record** that carries rich context for investigation

## Architecture

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

## Quick Start

```bash
docker compose up -d
```

## Raise an Alert

```bash
curl -X POST http://localhost:8081/raise \
  -H 'Content-Type: application/json' \
  -d '{
    "alert_name": "HighLatency",
    "severity": "critical",
    "reason": "p99 latency exceeded 500ms"
  }'
```

## Clear an Alert

```bash
curl -X POST http://localhost:8081/clear \
  -H 'Content-Type: application/json' \
  -d '{"alert_id": "<alert_id>"}'
```

## Exposed Ports

| Port | Service |
|---|---|
| 3000 | Grafana (admin/admin) |
| 8081–8083 | APAC workloads |
| 8084–8086 | EU workloads |
| 8087–8089 | US workloads |
| 9093 | APAC Alertmanager |
| 9094 | EU Alertmanager |
| 9095 | US Alertmanager |

## Documentation

Full documentation is available via [MkDocs](https://www.mkdocs.org/):

```bash
pip install mkdocs
mkdocs serve
```

Then open [http://localhost:8000](http://localhost:8000).

## Tear Down

```bash
docker compose down        # stop containers
docker compose down -v     # stop and remove volumes
```
