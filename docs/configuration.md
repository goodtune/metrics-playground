# Configuration Reference

## Repository Layout

```text
.
├── docker-compose.yml
├── mkdocs.yml
├── README.md
├── apps/
│   └── alert-simulator/
│       ├── Dockerfile
│       ├── requirements.txt
│       └── app.py
├── config/
│   ├── apac/
│   │   ├── otel-1/config.yaml
│   │   ├── otel-2/config.yaml
│   │   ├── otel-3/config.yaml
│   │   ├── vmagent/scrape.yaml
│   │   ├── vmalert/rules.yaml
│   │   └── alertmanager/alertmanager.yml
│   ├── eu/
│   │   └── (same structure)
│   └── us/
│       └── (same structure)
├── docs/
│   └── (mkdocs documentation)
└── grafana/
    ├── provisioning/
    │   ├── datasources/datasources.yaml
    │   └── dashboards/dashboards.yaml
    └── dashboards/
        ├── global-alert-overview.json
        ├── regional-service-health.json
        └── alert-context.json
```

## Config File Locations

### OpenTelemetry Collectors

| Service | Config Path |
|---|---|
| apac-otel-{1,2,3} | `config/apac/otel-{1,2,3}/config.yaml` |
| eu-otel-{1,2,3} | `config/eu/otel-{1,2,3}/config.yaml` |
| us-otel-{1,2,3} | `config/us/otel-{1,2,3}/config.yaml` |

Mounted to `/etc/otelcol/config.yaml` inside the container.

### vmagent

| Service | Config Path |
|---|---|
| apac-vmagent | `config/apac/vmagent/scrape.yaml` |
| eu-vmagent | `config/eu/vmagent/scrape.yaml` |
| us-vmagent | `config/us/vmagent/scrape.yaml` |

Mounted to `/etc/vmagent/` inside the container.

### vmalert

| Service | Config Path |
|---|---|
| apac-vmalert | `config/apac/vmalert/rules.yaml` |
| eu-vmalert | `config/eu/vmalert/rules.yaml` |
| us-vmalert | `config/us/vmalert/rules.yaml` |

Mounted to `/etc/vmalert/` inside the container.

### Alertmanager

| Service | Config Path |
|---|---|
| apac-alertmanager | `config/apac/alertmanager/alertmanager.yml` |
| eu-alertmanager | `config/eu/alertmanager/alertmanager.yml` |
| us-alertmanager | `config/us/alertmanager/alertmanager.yml` |

Mounted to `/etc/alertmanager/` inside the container.

### Grafana

| Purpose | Config Path |
|---|---|
| Datasources | `grafana/provisioning/datasources/datasources.yaml` |
| Dashboard provider | `grafana/provisioning/dashboards/dashboards.yaml` |
| Dashboard JSON files | `grafana/dashboards/*.json` |

## Volumes

### Persistent Named Volumes

| Volume | Container Mount | Purpose |
|---|---|---|
| `{region}_otel_{n}_data` | `/var/lib/otelcol` | Collector persistent queue |
| `{region}_vmagent_data` | `/vmagent-data` | vmagent relay buffer |
| `{region}_vm_data` | `/victoria-metrics-data` | Time-series storage |
| `{region}_vlogs_data` | `/victoria-logs-data` | Log storage |
| `{region}_vmalert_data` | `/vmalert-data` | vmalert state |
| `{region}_alertmanager_data` | `/alertmanager-data` | Silences and state |
| `grafana_data` | `/var/lib/grafana` | Grafana state |

28 volumes total across the 34 services.

## Environment Variables (Workloads)

| Variable | Example | Description |
|---|---|---|
| `REGION` | `apac` | Region identifier |
| `SERVICE_NAME` | `apac-app-1` | Service name for telemetry |
| `COMPONENT` | `workload` | Component label |
| `INSTANCE` | `apac-app-1` | Instance identifier |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://apac-otel-1:4318` | Local collector endpoint |
