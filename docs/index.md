# Metrics Playground

A local, Docker Compose-based lab environment for evaluating an event-based alerting architecture built around OpenTelemetry, VictoriaMetrics, VictoriaLogs, vmalert, Alertmanager, and Grafana.

## What This Lab Does

The lab lets you experience the full alert lifecycle end to end:

- Raise an alert from a simple workload-facing API
- Emit **two signals from one event**: a **metric** for alert evaluation and a **log record** for rich context
- Forward telemetry through a **local OpenTelemetry Collector** per workload
- Route metrics to a **regional metrics stack** (vmagent → VictoriaMetrics)
- Route logs to a **regional log backend** (VictoriaLogs)
- Evaluate alert rules with **vmalert**
- Route active alerts through **regional Alertmanager**
- Visualise all regions from a **single Grafana** instance
- Test failure modes by stopping individual containers

## Design Principles

1. **Preserve the event abstraction** — workloads call `raise`/`clear`, not backend-specific APIs
2. **Metric + log from one event** — metrics drive the firing decision, logs provide richer context
3. **Local collectors with buffering** — upstream outages can be simulated realistically
4. **Regional separation** — APAC, EU, and US regions are independently exercisable
5. **Single Grafana** — global review surface across all regions

## Quick Start

See the [Getting Started](getting-started.md) guide.
