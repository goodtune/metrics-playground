"""Alert Simulator workload for the observability lab.

Exposes a simple HTTP API to raise and clear alerts. Each event emits
both a metric (lab_alert_active gauge) and a log record via OTLP.
Includes a Datastar-powered UI with SSE-driven live updates.

Pre-registers all (alert_name × severity) metric series at startup so
VictoriaMetrics can index them before any alerts are raised, avoiding
the 5-10s new-series indexing delay.
"""

import html as html_mod
import logging
import os
import time
import uuid

import psutil
from flask import Flask, jsonify, request, Response
from opentelemetry import metrics
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Resource
# ---------------------------------------------------------------------------
REGION = os.environ.get("REGION", "unknown")
SERVICE_NAME = os.environ.get("SERVICE_NAME", "alert-simulator")
COMPONENT = os.environ.get("COMPONENT", "workload")
INSTANCE = os.environ.get("INSTANCE", "unknown")
OTLP_ENDPOINT = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
OTLP_METRICS_ENDPOINT = os.environ.get("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", OTLP_ENDPOINT)

resource = Resource.create(
    {
        "service.name": SERVICE_NAME,
        "region": REGION,
        "component": COMPONENT,
        "instance": INSTANCE,
    }
)

# ---------------------------------------------------------------------------
# Metrics setup
# ---------------------------------------------------------------------------
metric_exporter = OTLPMetricExporter(endpoint=f"{OTLP_METRICS_ENDPOINT}/v1/metrics")
metric_reader = PeriodicExportingMetricReader(metric_exporter, export_interval_millis=1000)
meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
metrics.set_meter_provider(meter_provider)
meter = metrics.get_meter("alert-simulator")

# ---------------------------------------------------------------------------
# Logging setup (OTLP)
# ---------------------------------------------------------------------------
log_exporter = OTLPLogExporter(endpoint=f"{OTLP_ENDPOINT}/v1/logs")
logger_provider = LoggerProvider(resource=resource)
logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))

otel_handler = LoggingHandler(level=logging.INFO, logger_provider=logger_provider)
alert_logger = logging.getLogger("alert.events")
alert_logger.addHandler(otel_handler)
alert_logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Pre-registered alert series
# ---------------------------------------------------------------------------
ALERT_NAMES = [
    "HighLatency", "DiskPressure", "MemoryExhausted", "CPUThrottle",
    "ConnectionPoolFull", "QueueBacklog", "CertExpiring", "ErrorRateSpike",
    "SlowQueries", "ReplicationLag",
]
SEVERITIES = ["critical", "warning", "info"]


def _make_alert_id(alert_name: str, severity: str) -> str:
    """Deterministic alert_id from (service, alert_name, severity)."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{SERVICE_NAME}.{alert_name}.{severity}"))


# Gauge values: key=(region, service, component, instance, alert_name, severity, alert_id) -> 0 or 1
# Pre-populated with ALL (name × severity) at value 0 so VM indexes them at startup.
alert_gauge_values: dict[tuple, int] = {}
for _name in ALERT_NAMES:
    for _sev in SEVERITIES:
        _aid = _make_alert_id(_name, _sev)
        _key = (REGION, SERVICE_NAME, COMPONENT, INSTANCE, _name, _sev, _aid)
        alert_gauge_values[_key] = 0

# Active alerts metadata
active_alerts: dict[str, dict] = {}


def _observe_alerts(_options):
    """Callback for the observable gauge."""
    for labels, value in list(alert_gauge_values.items()):
        yield metrics.Observation(
            value,
            {
                "region": labels[0],
                "service": labels[1],
                "component": labels[2],
                "instance": labels[3],
                "alert_name": labels[4],
                "severity": labels[5],
                "alert_id": labels[6],
                "source": "lab-api",
            },
        )


meter.create_observable_gauge(
    name="lab_alert_active",
    description="1 when alert is active, 0 when cleared",
    callbacks=[_observe_alerts],
)

# ---------------------------------------------------------------------------
# Process metrics
# ---------------------------------------------------------------------------
_process = psutil.Process()
_start_time = time.time()


def _observe_process_cpu(_options):
    yield metrics.Observation(_process.cpu_percent(), {})


def _observe_process_memory(_options):
    yield metrics.Observation(_process.memory_info().rss, {})


def _observe_process_threads(_options):
    yield metrics.Observation(_process.num_threads(), {})


def _observe_uptime(_options):
    yield metrics.Observation(time.time() - _start_time, {})


meter.create_observable_gauge(
    name="process_cpu_percent",
    description="Process CPU usage percentage",
    callbacks=[_observe_process_cpu],
)
meter.create_observable_gauge(
    name="process_memory_rss_bytes",
    description="Process resident set size in bytes",
    callbacks=[_observe_process_memory],
)
meter.create_observable_gauge(
    name="process_threads",
    description="Number of process threads",
    callbacks=[_observe_process_threads],
)
meter.create_observable_gauge(
    name="process_uptime_seconds",
    description="Seconds since process started",
    callbacks=[_observe_uptime],
)

http_request_counter = meter.create_counter(
    name="http_requests_total",
    description="Total HTTP requests served",
)

# ---------------------------------------------------------------------------
# Round-trip latency tracking
# ---------------------------------------------------------------------------
roundtrip_values: dict[tuple, float] = {}


def _observe_roundtrip(_options):
    """Callback for the round-trip latency gauge."""
    for labels, value in list(roundtrip_values.items()):
        yield metrics.Observation(
            value,
            {
                "region": labels[0],
                "alert_name": labels[1],
                "severity": labels[2],
                "alert_id": labels[3],
            },
        )


meter.create_observable_gauge(
    name="lab_alert_roundtrip_seconds",
    description="Seconds from alert raise to Alertmanager webhook delivery",
    callbacks=[_observe_roundtrip],
)


# ---------------------------------------------------------------------------
# SSE / Datastar helpers
# ---------------------------------------------------------------------------
def _render_alerts_fragment() -> str:
    """Render the alerts table as an HTML fragment for Datastar."""
    entries = sorted(
        active_alerts.values(), key=lambda a: a["event_time"], reverse=True
    )
    count = len(entries)

    if not entries:
        inner = '<div class="empty">No active alerts</div>'
    else:
        rows = ""
        for a in entries:
            ago = int(time.time() - a["event_time"])
            t = f"{ago}s" if ago < 60 else f"{ago // 60}m"
            aid = html_mod.escape(a["alert_id"])
            rows += (
                "<tr>"
                f'<td>{html_mod.escape(a["alert_name"])}</td>'
                f'<td class="sev-{html_mod.escape(a["severity"])}">'
                f'{html_mod.escape(a["severity"])}</td>'
                f'<td style="font-family:monospace;font-size:.7rem">{aid[:8]}</td>'
                f"<td>{t} ago</td>"
                f'<td><button class="btn-clear" '
                f"""data-on:click="@post('/alerts/clear/{aid}')">"""
                f"Clear</button></td>"
                "</tr>"
            )
        inner = (
            "<table><tr><th>Name</th><th>Severity</th><th>ID</th>"
            f"<th>Time</th><th></th></tr>{rows}</table>"
        )

    count_text = f" ({count})" if count else ""
    return (
        f'<div id="alerts-list">{inner}</div>'
        f'<span id="alerts-count">{count_text}</span>'
    )


def _sse_response(*fragments: str) -> Response:
    """Build a Datastar SSE response that patches one or more fragments."""
    body = ""
    for frag in fragments:
        body += "event: datastar-patch-elements\n"
        body += f"data: elements {frag}\n"
        body += "\n"
    return Response(body, content_type="text/event-stream")


# ---------------------------------------------------------------------------
# Alert mutation helpers (shared by Datastar + JSON routes)
# ---------------------------------------------------------------------------
def _do_raise(alert_name, severity, reason, message, correlation_id):
    """Raise an alert by toggling its pre-registered gauge from 0 to 1."""
    alert_id = _make_alert_id(alert_name, severity)
    key = (REGION, SERVICE_NAME, COMPONENT, INSTANCE, alert_name, severity, alert_id)

    if key not in alert_gauge_values:
        # Unregistered combination — add it (will take longer first time)
        alert_gauge_values[key] = 0

    if alert_id in active_alerts:
        # Already active with this name+severity — update event_time for re-raise
        active_alerts[alert_id]["event_time"] = time.time()
        return active_alerts[alert_id]

    alert_gauge_values[key] = 1
    http_request_counter.add(1, {"method": "POST", "endpoint": "/raise"})

    alert_record = {
        "alert_id": alert_id,
        "alert_name": alert_name,
        "state": "raised",
        "region": REGION,
        "service": SERVICE_NAME,
        "component": COMPONENT,
        "instance": INSTANCE,
        "severity": severity,
        "reason": reason,
        "alert_message": message,
        "correlation_id": correlation_id,
        "event_time": time.time(),
    }
    active_alerts[alert_id] = alert_record
    alert_logger.info("Alert raised: %s", alert_name, extra=alert_record)
    return alert_record


def _do_clear(alert_id, reason="manually cleared"):
    """Clear an alert by setting its gauge back to 0."""
    record = active_alerts.pop(alert_id, None)
    if record is None:
        return None

    key = (
        REGION, SERVICE_NAME, COMPONENT, INSTANCE,
        record["alert_name"], record["severity"], alert_id,
    )
    alert_gauge_values[key] = 0  # Set to 0, don't remove (keep series alive)
    roundtrip_key = (REGION, record["alert_name"], record["severity"], alert_id)
    roundtrip_values.pop(roundtrip_key, None)
    http_request_counter.add(1, {"method": "POST", "endpoint": "/clear"})

    alert_logger.info("Alert cleared: %s", record["alert_name"], extra={
        "alert_id": alert_id,
        "alert_name": record["alert_name"],
        "state": "cleared",
        "region": REGION,
        "service": SERVICE_NAME,
        "component": COMPONENT,
        "instance": INSTANCE,
        "severity": record["severity"],
        "reason": reason,
        "correlation_id": record.get("correlation_id", ""),
        "event_time": time.time(),
    })
    return record


# ---------------------------------------------------------------------------
# UI template
# ---------------------------------------------------------------------------
_INDEX_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Alert Simulator &middot; __INSTANCE__</title>
<script type="module" src="https://cdn.jsdelivr.net/gh/starfederation/datastar@v1.0.0-RC.8/bundles/datastar.js"></script>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: system-ui, -apple-system, sans-serif; background: #0f1117; color: #e1e4e8; padding: 1.5rem; max-width: 720px; margin: 0 auto; }
  h1 { font-size: 1.25rem; font-weight: 600; margin-bottom: .25rem; }
  .meta { font-size: .8rem; color: #8b949e; margin-bottom: 1.5rem; }
  .meta span { background: #21262d; padding: 2px 8px; border-radius: 4px; margin-right: .5rem; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 1.25rem; margin-bottom: 1rem; }
  .card h2 { font-size: .95rem; font-weight: 600; margin-bottom: 1rem; color: #c9d1d9; }
  label { display: block; font-size: .8rem; color: #8b949e; margin-bottom: .25rem; }
  input, select { width: 100%; padding: .5rem; background: #0d1117; border: 1px solid #30363d; border-radius: 6px; color: #e1e4e8; font-size: .85rem; margin-bottom: .75rem; }
  input:focus, select:focus { outline: none; border-color: #58a6ff; }
  .row { display: flex; gap: .75rem; }
  .row > * { flex: 1; }
  button { padding: .5rem 1rem; border: none; border-radius: 6px; font-size: .85rem; font-weight: 500; cursor: pointer; transition: opacity .15s; }
  button:hover { opacity: .85; }
  .btn-raise { background: #da3633; color: #fff; }
  .btn-clear { background: #238636; color: #fff; font-size: .75rem; padding: .3rem .6rem; }
  .btn-clear-all { background: #30363d; color: #c9d1d9; }
  .actions { display: flex; gap: .5rem; margin-top: .25rem; }
  table { width: 100%; border-collapse: collapse; font-size: .8rem; }
  th { text-align: left; padding: .5rem; border-bottom: 1px solid #30363d; color: #8b949e; font-weight: 500; }
  td { padding: .5rem; border-bottom: 1px solid #21262d; }
  .sev-critical { color: #f85149; }
  .sev-warning { color: #d29922; }
  .sev-info { color: #58a6ff; }
  .empty { color: #484f58; font-style: italic; padding: 1rem; text-align: center; }
</style>
</head>
<body>
  <h1>Alert Simulator</h1>
  <div class="meta">
    <span>__REGION__</span>
    <span>__SERVICE__</span>
    <span>__INSTANCE__</span>
  </div>

  <div class="card"
       data-signals="{alertName: 'HighLatency', severity: 'warning', message: '', reason: '', correlationId: ''}">
    <h2>Raise Alert</h2>
    <div class="row">
      <div>
        <label>Alert Name</label>
        <input data-bind:alertName />
      </div>
      <div>
        <label>Severity</label>
        <select data-bind:severity>
          <option value="critical">critical</option>
          <option value="warning" selected>warning</option>
          <option value="info">info</option>
        </select>
      </div>
    </div>
    <label>Message</label>
    <input data-bind:message placeholder="optional">
    <div class="row">
      <div><label>Reason</label><input data-bind:reason placeholder="optional"></div>
      <div><label>Correlation ID</label><input data-bind:correlationId placeholder="optional"></div>
    </div>
    <div class="actions">
      <button class="btn-raise" data-on:click="@post('/alerts/raise')">Raise Alert</button>
    </div>
  </div>

  <div class="card" data-on:load="@get('/alerts/feed')">
    <h2>Active Alerts<span id="alerts-count"></span></h2>
    <div id="alerts-list"><div class="empty">Loading...</div></div>
    <div class="actions" style="margin-top:.75rem">
      <button class="btn-clear-all" data-on:click="@post('/alerts/clear-all')">Clear All</button>
    </div>
  </div>

</body>
</html>
"""

INDEX_HTML = (
    _INDEX_TEMPLATE
    .replace("__REGION__", html_mod.escape(REGION))
    .replace("__SERVICE__", html_mod.escape(SERVICE_NAME))
    .replace("__INSTANCE__", html_mod.escape(INSTANCE))
)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return Response(INDEX_HTML, content_type="text/html")


@app.route("/health", methods=["GET"])
def health():
    http_request_counter.add(1, {"method": "GET", "endpoint": "/health"})
    return jsonify({"status": "ok"})


@app.route("/alerts", methods=["GET"])
def list_alerts():
    http_request_counter.add(1, {"method": "GET", "endpoint": "/alerts"})
    return jsonify(active_alerts)


@app.route("/alerts/feed", methods=["GET"])
def alerts_feed():
    """SSE endpoint: sends current alerts fragment."""
    return _sse_response(_render_alerts_fragment())


@app.route("/alerts/raise", methods=["POST"])
def ds_raise_alert():
    """Datastar action: raise an alert and return updated fragment."""
    data = request.get_json(force=True, silent=True) or {}
    _do_raise(
        alert_name=data.get("alertName") or data.get("alert_name") or "TestAlert",
        severity=data.get("severity", "warning"),
        reason=data.get("reason", ""),
        message=data.get("message", ""),
        correlation_id=data.get("correlationId") or data.get("correlation_id") or "",
    )
    return _sse_response(_render_alerts_fragment())


@app.route("/alerts/clear/<alert_id>", methods=["POST"])
def ds_clear_alert(alert_id):
    """Datastar action: clear a single alert and return updated fragment."""
    _do_clear(alert_id)
    return _sse_response(_render_alerts_fragment())


@app.route("/alerts/clear-all", methods=["POST"])
def ds_clear_all():
    """Datastar action: clear every active alert."""
    for aid in list(active_alerts.keys()):
        _do_clear(aid, reason="bulk clear")
    return _sse_response(_render_alerts_fragment())


# Original JSON API endpoints for programmatic use
@app.route("/raise", methods=["POST"])
def raise_alert():
    data = request.get_json(force=True, silent=True) or {}
    record = _do_raise(
        alert_name=data.get("alert_name", "TestAlert"),
        severity=data.get("severity", "warning"),
        reason=data.get("reason", ""),
        message=data.get("message", ""),
        correlation_id=data.get("correlation_id", ""),
    )
    if record is None:
        return jsonify({"error": "failed to raise alert"}), 500
    return jsonify({"status": "raised", "alert_id": record["alert_id"]}), 201


@app.route("/clear", methods=["POST"])
def clear_alert():
    data = request.get_json(force=True, silent=True) or {}
    alert_id = data.get("alert_id", "")
    if not alert_id:
        return jsonify({"error": "alert_id is required"}), 400

    record = _do_clear(alert_id, reason=data.get("reason", "manually cleared"))
    if record is None:
        return jsonify({"error": "alert not found"}), 404

    return jsonify({"status": "cleared", "alert_id": alert_id})


@app.route("/webhook", methods=["POST"])
def alertmanager_webhook():
    """Receive Alertmanager webhook and compute round-trip latency."""
    now = time.time()
    data = request.get_json(force=True, silent=True) or {}
    results = []
    for alert in data.get("alerts", []):
        labels = alert.get("labels", {})
        alert_id = labels.get("alert_id", "")
        record = active_alerts.get(alert_id)
        if record is None:
            continue
        key = (REGION, record["alert_name"], record["severity"], alert_id)
        if key in roundtrip_values:
            continue
        latency = now - record["event_time"]
        roundtrip_values[key] = latency
        print(f"[round-trip] {record['alert_name']}: {latency:.3f}s", flush=True)
        alert_logger.info(
            "Alert round-trip: %s in %.3fs",
            record["alert_name"],
            latency,
            extra={
                "alert_id": alert_id,
                "alert_name": record["alert_name"],
                "severity": record["severity"],
                "region": REGION,
                "roundtrip_seconds": latency,
                "state": "webhook_received",
            },
        )
        results.append({"alert_id": alert_id, "roundtrip_seconds": round(latency, 3)})
    return jsonify({"processed": len(results), "results": results})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
