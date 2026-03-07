# Grafana

## Role

Grafana is the **global viewing plane**. A single instance connects to all three regional backends, providing cross-region visibility.

## Datasources

Provisioned automatically on startup:

| Datasource | Type | URL |
|---|---|---|
| victoriametrics-apac | Prometheus | `http://apac-victoriametrics:8428` |
| victoriametrics-eu | Prometheus | `http://eu-victoriametrics:8428` |
| victoriametrics-us | Prometheus | `http://us-victoriametrics:8428` |
| victorialogs-apac | VictoriaLogs | `http://apac-victorialogs:9428` |
| victorialogs-eu | VictoriaLogs | `http://eu-victorialogs:9428` |
| victorialogs-us | VictoriaLogs | `http://us-victorialogs:9428` |

## Dashboards

Three dashboards are provisioned from `grafana/dashboards/`:

### Global Alert Overview

- Active alerts by region (stat panels)
- Active alerts by severity (table)
- Active alerts by service (table)
- Active alert count over time (time series)

### Regional Service Health

- Template variable for region selection (apac, eu, us)
- `lab_alert_active` by service/component/instance
- Alert timeline by alert_name

### Alert Context (Logs)

- Template variable for region selection
- Recent alert event logs from VictoriaLogs
- Filterable by alert attributes

## Access

- **URL**: [http://localhost:3000](http://localhost:3000)
- **Username**: admin
- **Password**: admin

## Persistence

- Grafana state persisted to `grafana_data` volume at `/var/lib/grafana`
- Provisioning files bind-mounted read-only from `grafana/provisioning/`
- Dashboard JSON files bind-mounted read-only from `grafana/dashboards/`

## Plugins

The `victoriametrics-logs-datasource` plugin is installed automatically via the `GF_INSTALL_PLUGINS` environment variable.
