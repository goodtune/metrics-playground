# Metrics Playground

A local, Docker Compose-based lab for evaluating event-based alerting with OpenTelemetry, VictoriaMetrics, VictoriaLogs, vmalert, Alertmanager, Grafana, and an operator Alert Dashboard.

## Overview

This lab models a three-region (APAC, EU, US) observability stack with **36 services**. Each region has 3 workloads, 3 local OTEL collectors, a vmagent, VictoriaMetrics, VictoriaLogs, vmalert, and Alertmanager. Global services include Grafana for dashboarding and an Alert Dashboard backed by PostgreSQL for operational alert management with real-time SSE updates via Datastar.

One event produces two signals:

- A **metric** (`lab_alert_active` gauge) that drives the alerting decision
- A **log record** that carries rich context for investigation

## Architecture

### Regional Architecture (identical per region)

```mermaid
flowchart LR
  W[3 Workloads] --> C[3 OTEL Collectors]
  C -->|OTLP metrics| VA[vmagent] -->|remote_write| VM[VictoriaMetrics]
  C -->|OTLP logs| VL[VictoriaLogs]
  VAL[vmalert] -->|query rules| VM
  VAL -->|active alerts| AM[Alertmanager]
```

### Global Integration

```mermaid
flowchart LR
  APAC[APAC Region] -->|datasources| G[Grafana]
  EU[Europe Region] -->|datasources| G
  US[US Region] -->|datasources| G
  APAC -->|webhook| AD[Alert Dashboard]
  EU -->|webhook| AD
  US -->|webhook| AD
  DB[(PostgreSQL)] --- AD
  AD -->|SSE live updates| Browser((Operator Browser))
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
| 8090 | Alert Dashboard |
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
