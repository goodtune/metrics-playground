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

The lab uses a minimal configuration with a default route and a null (`blackhole`) receiver:

```yaml
global:
  resolve_timeout: 5m

route:
  receiver: blackhole
  group_by:
    - region
    - service
    - alert_name
  group_wait: 10s
  group_interval: 30s
  repeat_interval: 1h

receivers:
  - name: blackhole
```

### Extending for Notifications

To add downstream notification, define additional receivers (webhook, PagerDuty, Slack, email) and update the route tree:

```yaml
receivers:
  - name: blackhole
  - name: webhook
    webhook_configs:
      - url: http://your-webhook-endpoint:5001/
```

## Exposed Ports

| Port | Region |
|---|---|
| 9093 | APAC |
| 9094 | EU |
| 9095 | US |

## Persistence

Each Alertmanager has a persistent volume at `/alertmanager-data` for silences and operational state.
