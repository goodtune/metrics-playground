# Alertmanager

## Role

Alertmanager manages the lifecycle of alerts **after** they are fired by vmalert. It handles grouping, deduplication, silencing, inhibition, routing, and notifications.

## What It Does

- Receives active alerts from regional vmalert
- Deduplicates repeated updates for the same alert identity
- Groups alerts for downstream presentation
- Supports silences and inhibition rules
- Exposes a web UI for inspection
- Routes notifications to configured receivers

## What It Does Not Do

Alertmanager does **not** query metrics or evaluate alert rules. That is vmalert's responsibility.

## Configuration

Config mounted from `config/<region>/alertmanager/alertmanager.yml`.

The lab uses a tuned configuration with per-alert grouping, fast wait times, and webhook receivers that route alerts back to each workload for round-trip measurement:

```yaml
global:
  resolve_timeout: 1m

route:
  receiver: <region>-app-1
  group_by: ['region', 'service', 'alert_name', 'severity', 'alert_id']
  group_wait: 1s
  group_interval: 1s
  repeat_interval: 1m
  routes:
    - receiver: <region>-app-2
      matchers:
        - service="<region>-app-2"
    - receiver: <region>-app-3
      matchers:
        - service="<region>-app-3"

receivers:
  - name: <region>-app-1
    webhook_configs:
      - url: http://<region>-app-1:8080/webhook
        send_resolved: false
  - name: <region>-app-2
    webhook_configs:
      - url: http://<region>-app-2:8080/webhook
        send_resolved: false
  - name: <region>-app-3
    webhook_configs:
      - url: http://<region>-app-3:8080/webhook
        send_resolved: false
```

Key tuning choices:

- `group_by` includes `alert_id` for per-alert tracking (see [Performance Tuning](../performance.md#6-alertmanager-per-alert-grouping))
- `group_wait: 1s` and `group_interval: 1s` minimise notification delay
- `send_resolved: false` — the lab only measures raise round-trips

## Exposed Ports

| Port | Region |
|---|---|
| 9093 | APAC |
| 9094 | EU |
| 9095 | US |

## Persistence

Each Alertmanager has a persistent volume at `/alertmanager-data` for silences and operational state.
