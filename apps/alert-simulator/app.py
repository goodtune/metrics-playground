"""Alert Simulator workload for the observability lab.

Exposes a simple HTTP API to raise and clear alerts. Each event emits
both a metric (lab_alert_active gauge) and a log record via OTLP.
"""

import logging
import os
import time
import uuid

from flask import Flask, jsonify, request
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
metric_exporter = OTLPMetricExporter(endpoint=f"{OTLP_ENDPOINT}/v1/metrics")
metric_reader = PeriodicExportingMetricReader(metric_exporter, export_interval_millis=5000)
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
# Alert state tracking
# ---------------------------------------------------------------------------
# key: (alert_name, service, component, instance) -> gauge value
active_alerts: dict[str, dict] = {}

# We use an UpDownCounter-like pattern via ObservableGauge callbacks.
# Store current values and let the callback read them.
alert_gauge_values: dict[tuple, int] = {}


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
# API routes
# ---------------------------------------------------------------------------


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/alerts", methods=["GET"])
def list_alerts():
    return jsonify(active_alerts)


@app.route("/raise", methods=["POST"])
def raise_alert():
    data = request.get_json(force=True, silent=True) or {}

    alert_id = data.get("alert_id", str(uuid.uuid4()))
    alert_name = data.get("alert_name", "TestAlert")
    severity = data.get("severity", "warning")
    reason = data.get("reason", "")
    message = data.get("message", "")
    correlation_id = data.get("correlation_id", "")

    key = (REGION, SERVICE_NAME, COMPONENT, INSTANCE, alert_name, severity, alert_id)
    alert_gauge_values[key] = 1

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
        "message": message,
        "correlation_id": correlation_id,
        "event_time": time.time(),
    }
    active_alerts[alert_id] = alert_record

    alert_logger.info(
        "Alert raised: %s",
        alert_name,
        extra=alert_record,
    )

    return jsonify({"status": "raised", "alert_id": alert_id}), 201


@app.route("/clear", methods=["POST"])
def clear_alert():
    data = request.get_json(force=True, silent=True) or {}

    alert_id = data.get("alert_id", "")
    if not alert_id:
        return jsonify({"error": "alert_id is required"}), 400

    record = active_alerts.pop(alert_id, None)
    if record is None:
        return jsonify({"error": "alert not found"}), 404

    key = (
        REGION,
        SERVICE_NAME,
        COMPONENT,
        record.get("instance", INSTANCE),
        record["alert_name"],
        record["severity"],
        alert_id,
    )
    alert_gauge_values[key] = 0

    clear_record = {
        "alert_id": alert_id,
        "alert_name": record["alert_name"],
        "state": "cleared",
        "region": REGION,
        "service": SERVICE_NAME,
        "component": COMPONENT,
        "instance": INSTANCE,
        "severity": record["severity"],
        "reason": data.get("reason", "manually cleared"),
        "message": data.get("message", ""),
        "correlation_id": record.get("correlation_id", ""),
        "event_time": time.time(),
    }

    alert_logger.info(
        "Alert cleared: %s",
        record["alert_name"],
        extra=clear_record,
    )

    return jsonify({"status": "cleared", "alert_id": alert_id})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
