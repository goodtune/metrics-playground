# VictoriaMetrics Stack

## Components

The VictoriaMetrics stack in each region consists of three components:

| Component | Role |
|---|---|
| **vmagent** | Regional metrics ingress and forwarder |
| **VictoriaMetrics** | Time-series storage and query engine |
| **vmalert** | Rule evaluation engine |

## vmagent

### Role

Accepts incoming metrics from local collectors and forwards them to VictoriaMetrics via Prometheus `remote_write`.

### Configuration

- Accepts metrics on port `8429`
- Forwards to `http://<region>-victoriametrics:8428/api/v1/write`
- Config mounted from `config/<region>/vmagent/scrape.yaml`
- Persistent volume at `/vmagent-data`

### What It Does Not Do

vmagent does not evaluate alert rules or make alerting decisions.

## VictoriaMetrics

### Role

Regional time-series database. Stores metrics for dashboarding and alert rule evaluation.

### Configuration

- HTTP API on port `8428`
- 30-day retention
- Persistent volume at `/victoria-metrics-data`

### Queried By

- **Grafana** — for dashboards
- **vmalert** — for alert rule evaluation

## vmalert

### Role

Evaluates alerting rules against VictoriaMetrics on a schedule and sends active alerts to Alertmanager.

!!! important
    vmalert makes the fire/not-fire decision. Alertmanager does **not** evaluate metrics — it receives alerts from vmalert and manages their lifecycle.

### Configuration

- Queries `http://<region>-victoriametrics:8428`
- Notifies `http://<region>-alertmanager:9093`
- 1-second evaluation interval
- Rules mounted from `config/<region>/vmalert/rules.yaml`
- Persistent volume at `/vmalert-data`

### Alert Rule

```yaml
groups:
  - name: lab-alerts
    interval: 1s
    rules:
      - alert: LabAlertActive
        expr: >-
          max by (region, service, component, instance,
                  alert_id, alert_name, severity)
          (lab_alert_active == 1)
        for: 0s
        labels:
          routing: regional
        annotations:
          summary: "{{ $labels.alert_name }} active on {{ $labels.instance }}"
          description: "Alert {{ $labels.alert_id }} is currently active"
```

## VictoriaLogs

### Role

Regional log backend. Receives OTLP logs from local collectors and provides query access via Grafana.

### Configuration

- HTTP API on port `9428`
- OTLP log ingestion enabled
- 30-day retention
- Persistent volume at `/victoria-logs-data`
