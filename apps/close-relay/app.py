"""Close Relay - emits synthetic close metrics for event-based alerts.

Receives close requests from the Alert Dashboard and emits a
lab_alert_closed gauge (value = unix timestamp) via OTLP. One instance
runs per region, connecting to the regional OTEL collector.
"""

import os
import time

from flask import Flask, jsonify, request
from opentelemetry import metrics
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource

app = Flask(__name__)

REGION = os.environ.get("REGION", "unknown")
SERVICE_NAME = f"{REGION}-close-relay"
OTLP_ENDPOINT = os.environ.get(
    "OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318"
)

resource = Resource.create(
    {
        "service.name": SERVICE_NAME,
        "region": REGION,
        "component": "close-relay",
    }
)

metric_exporter = OTLPMetricExporter(
    endpoint=f"{OTLP_ENDPOINT}/v1/metrics"
)
metric_reader = PeriodicExportingMetricReader(
    metric_exporter, export_interval_millis=1000
)
meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
metrics.set_meter_provider(meter_provider)
meter = metrics.get_meter("close-relay")

close_gauge = meter.create_gauge(
    name="lab_alert_closed",
    description="Unix timestamp of operator close for event-based alerts",
)

# Pre-register lab_alert_closed series (value 1 = "considered closed by
# default") so VictoriaMetrics indexes them at startup.  Value 1 is always
# less than a real Unix timestamp, so pre-registered entries suppress the
# vmalert rule (1 >= 0 for pre-registered raised) without causing false
# negatives on real raises (1 < real_timestamp).
ALERT_NAMES = [
    "HighLatency", "DiskPressure", "MemoryExhausted", "CPUThrottle",
    "ConnectionPoolFull", "QueueBacklog", "CertExpiring", "ErrorRateSpike",
    "SlowQueries", "ReplicationLag",
]
SEVERITIES = ["critical", "warning", "info"]
SERVICES = [f"{REGION}-app-{i}" for i in (1, 2, 3)]

for _name in ALERT_NAMES:
    for _sev in SEVERITIES:
        for _svc in SERVICES:
            close_gauge.set(1, attributes={
                "alert_name": _name,
                "service": _svc,
                "severity": _sev,
                "region": REGION,
            })


@app.route("/close", methods=["POST"])
def close_alert():
    """Emit a lab_alert_closed metric for the given alert."""
    data = request.get_json(force=True, silent=True) or {}
    alert_name = data.get("alert_name", "")
    service = data.get("service", "")
    severity = data.get("severity", "warning")

    if not alert_name or not service:
        return jsonify({"error": "alert_name and service are required"}), 400

    now = time.time()
    close_gauge.set(
        now,
        attributes={
            "alert_name": alert_name,
            "service": service,
            "severity": severity,
            "region": REGION,
        },
    )

    return jsonify({
        "status": "closed",
        "alert_name": alert_name,
        "service": service,
        "severity": severity,
        "timestamp": now,
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
