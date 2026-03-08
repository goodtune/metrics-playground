"""Alert Dashboard - Operational alert management service.

Receives Alertmanager webhook notifications, stores alert state in PostgreSQL,
and serves a responsive operator dashboard. Operators can drill into metric and
log context and manually close alerts that lack auto-resolution, pushing a
synthetic clear back through the originating workload.
"""

import json
import os
import textwrap
import time
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
import requests
from flask import Flask, Response, jsonify, request

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DB_DSN = os.environ.get(
    "DATABASE_URL",
    "postgresql://alertdash:alertdash@alert-dashboard-db:5432/alertdash",
)

# VictoriaMetrics / VictoriaLogs endpoints per region
VM_ENDPOINTS = {
    "apac": os.environ.get("VM_APAC", "http://apac-victoriametrics:8428"),
    "eu": os.environ.get("VM_EU", "http://eu-victoriametrics:8428"),
    "us": os.environ.get("VM_US", "http://us-victoriametrics:8428"),
}
VL_ENDPOINTS = {
    "apac": os.environ.get("VL_APAC", "http://apac-victorialogs:9428"),
    "eu": os.environ.get("VL_EU", "http://eu-victorialogs:9428"),
    "us": os.environ.get("VL_US", "http://us-victorialogs:9428"),
}

# Map service names to their in-cluster HTTP address for pushing clears
# The alert-simulator apps all listen on port 8080 inside the network.
SERVICE_ENDPOINTS = {}
for region in ("apac", "eu", "us"):
    for i in (1, 2, 3):
        name = f"{region}-app-{i}"
        SERVICE_ENDPOINTS[name] = f"http://{name}:8080"

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def _get_conn():
    return psycopg2.connect(DB_DSN)


def _init_db():
    """Create tables if they don't exist."""
    ddl = textwrap.dedent("""\
        CREATE TABLE IF NOT EXISTS alerts (
            id              SERIAL PRIMARY KEY,
            fingerprint     TEXT NOT NULL,
            alert_name      TEXT NOT NULL,
            severity        TEXT NOT NULL DEFAULT 'warning',
            region          TEXT NOT NULL DEFAULT '',
            service         TEXT NOT NULL DEFAULT '',
            instance        TEXT NOT NULL DEFAULT '',
            alert_id        TEXT NOT NULL DEFAULT '',
            status          TEXT NOT NULL DEFAULT 'firing',
            summary         TEXT NOT NULL DEFAULT '',
            description     TEXT NOT NULL DEFAULT '',
            labels          JSONB NOT NULL DEFAULT '{}',
            annotations     JSONB NOT NULL DEFAULT '{}',
            starts_at       TIMESTAMPTZ,
            ends_at         TIMESTAMPTZ,
            received_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            resolved_at     TIMESTAMPTZ,
            resolved_by     TEXT,
            UNIQUE (fingerprint)
        );
        CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts (status);
        CREATE INDEX IF NOT EXISTS idx_alerts_region ON alerts (region);
        CREATE INDEX IF NOT EXISTS idx_alerts_service ON alerts (service);

        CREATE TABLE IF NOT EXISTS alert_history (
            id              SERIAL PRIMARY KEY,
            alert_id        INTEGER NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
            status          TEXT NOT NULL,
            received_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            raw_payload     JSONB
        );
    """)
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(ddl)
        conn.commit()


# Retry DB init on startup (PostgreSQL may not be ready yet)
for _attempt in range(30):
    try:
        _init_db()
        break
    except Exception as exc:
        if _attempt == 29:
            raise
        print(f"[init] DB not ready ({exc}), retrying in 1s ...", flush=True)
        time.sleep(1)


# ---------------------------------------------------------------------------
# Webhook receiver
# ---------------------------------------------------------------------------

@app.route("/webhook", methods=["POST"])
def alertmanager_webhook():
    """Receive Alertmanager webhook payload and upsert alert state."""
    payload = request.get_json(force=True, silent=True) or {}
    alerts = payload.get("alerts", [])
    processed = 0
    with _get_conn() as conn:
        with conn.cursor() as cur:
            for alert in alerts:
                fingerprint = alert.get("fingerprint", "")
                status = alert.get("status", "firing")
                labels = alert.get("labels", {})
                annotations = alert.get("annotations", {})
                starts_at = alert.get("startsAt")
                ends_at = alert.get("endsAt")

                alert_name = labels.get("alert_name", labels.get("alertname", ""))
                severity = labels.get("severity", "warning")
                region = labels.get("region", "")
                service = labels.get("service", "")
                instance = labels.get("instance", "")
                alert_id = labels.get("alert_id", "")
                summary = annotations.get("summary", "")
                description = annotations.get("description", "")

                cur.execute(
                    """\
                    INSERT INTO alerts
                        (fingerprint, alert_name, severity, region, service,
                         instance, alert_id, status, summary, description,
                         labels, annotations, starts_at, ends_at, received_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                    ON CONFLICT (fingerprint) DO UPDATE SET
                        status      = EXCLUDED.status,
                        summary     = EXCLUDED.summary,
                        description = EXCLUDED.description,
                        labels      = EXCLUDED.labels,
                        annotations = EXCLUDED.annotations,
                        ends_at     = EXCLUDED.ends_at,
                        received_at = NOW(),
                        resolved_at = CASE
                            WHEN EXCLUDED.status = 'resolved'
                            THEN NOW() ELSE alerts.resolved_at END
                    RETURNING id
                    """,
                    (
                        fingerprint, alert_name, severity, region, service,
                        instance, alert_id, status, summary, description,
                        json.dumps(labels), json.dumps(annotations),
                        starts_at, ends_at,
                    ),
                )
                row = cur.fetchone()
                if row:
                    cur.execute(
                        """\
                        INSERT INTO alert_history (alert_id, status, raw_payload)
                        VALUES (%s, %s, %s)
                        """,
                        (row[0], status, json.dumps(alert)),
                    )
                processed += 1
        conn.commit()
    return jsonify({"processed": processed})


# ---------------------------------------------------------------------------
# API endpoints for the dashboard UI
# ---------------------------------------------------------------------------

@app.route("/api/alerts", methods=["GET"])
def api_list_alerts():
    """Return alerts, optionally filtered by status/region/service."""
    status = request.args.get("status")
    region = request.args.get("region")
    service = request.args.get("service")

    clauses = []
    params = []
    if status:
        clauses.append("status = %s")
        params.append(status)
    if region:
        clauses.append("region = %s")
        params.append(region)
    if service:
        clauses.append("service = %s")
        params.append(service)

    where = "WHERE " + " AND ".join(clauses) if clauses else ""

    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"""\
                SELECT id, fingerprint, alert_name, severity, region, service,
                       instance, alert_id, status, summary, description,
                       labels, annotations,
                       starts_at, ends_at, received_at, resolved_at, resolved_by
                FROM alerts {where}
                ORDER BY
                    CASE status WHEN 'firing' THEN 0 ELSE 1 END,
                    CASE severity
                        WHEN 'critical' THEN 0
                        WHEN 'warning'  THEN 1
                        ELSE 2 END,
                    received_at DESC
                """,
                params,
            )
            rows = cur.fetchall()
    # Convert datetimes to ISO strings
    for row in rows:
        for k in ("starts_at", "ends_at", "received_at", "resolved_at"):
            if row[k] is not None:
                row[k] = row[k].isoformat()
    return jsonify(rows)


@app.route("/api/alerts/<int:alert_db_id>", methods=["GET"])
def api_get_alert(alert_db_id):
    """Get a single alert with its history."""
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM alerts WHERE id = %s", (alert_db_id,))
            alert = cur.fetchone()
            if not alert:
                return jsonify({"error": "not found"}), 404
            cur.execute(
                "SELECT * FROM alert_history WHERE alert_id = %s ORDER BY received_at",
                (alert_db_id,),
            )
            history = cur.fetchall()
    for obj in [alert] + history:
        for k in ("starts_at", "ends_at", "received_at", "resolved_at"):
            if k in obj and obj[k] is not None:
                obj[k] = obj[k].isoformat()
    alert["history"] = history
    return jsonify(alert)


@app.route("/api/alerts/<int:alert_db_id>/close", methods=["POST"])
def api_close_alert(alert_db_id):
    """Operator closes an alert. Pushes a synthetic clear to the originating app."""
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM alerts WHERE id = %s", (alert_db_id,))
            alert = cur.fetchone()
            if not alert:
                return jsonify({"error": "not found"}), 404
            if alert["status"] == "resolved":
                return jsonify({"error": "already resolved"}), 409

            # Push synthetic clear to the originating workload
            service = alert["service"]
            alert_id = alert["alert_id"]
            endpoint = SERVICE_ENDPOINTS.get(service)
            clear_error = None
            if endpoint and alert_id:
                try:
                    resp = requests.post(
                        f"{endpoint}/clear",
                        json={"alert_id": alert_id, "reason": "operator-dashboard-close"},
                        timeout=5,
                    )
                    if resp.status_code >= 400:
                        clear_error = f"upstream {resp.status_code}: {resp.text[:200]}"
                except Exception as exc:
                    clear_error = str(exc)

            # Mark resolved in our DB regardless (operator intent)
            cur.execute(
                """\
                UPDATE alerts
                SET status = 'resolved', resolved_at = NOW(), resolved_by = 'operator'
                WHERE id = %s
                """,
                (alert_db_id,),
            )
            cur.execute(
                "INSERT INTO alert_history (alert_id, status) VALUES (%s, 'resolved')",
                (alert_db_id,),
            )
        conn.commit()

    result = {"status": "resolved", "id": alert_db_id}
    if clear_error:
        result["clear_warning"] = clear_error
    return jsonify(result)


@app.route("/api/alerts/stats", methods=["GET"])
def api_alert_stats():
    """Summary statistics for the dashboard header."""
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""\
                SELECT
                    COUNT(*) FILTER (WHERE status = 'firing') AS firing,
                    COUNT(*) FILTER (WHERE status = 'resolved') AS resolved,
                    COUNT(*) FILTER (WHERE status = 'firing' AND severity = 'critical') AS critical,
                    COUNT(*) FILTER (WHERE status = 'firing' AND severity = 'warning') AS warning,
                    COUNT(*) FILTER (WHERE status = 'firing' AND severity = 'info') AS info
                FROM alerts
            """)
            row = cur.fetchone()
    return jsonify(row)


# ---------------------------------------------------------------------------
# Metric context (proxied from VictoriaMetrics)
# ---------------------------------------------------------------------------

@app.route("/api/alerts/<int:alert_db_id>/metrics", methods=["GET"])
def api_alert_metrics(alert_db_id):
    """Fetch metric context for an alert from the regional VictoriaMetrics."""
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM alerts WHERE id = %s", (alert_db_id,))
            alert = cur.fetchone()
    if not alert:
        return jsonify({"error": "not found"}), 404

    region = alert["region"]
    vm_url = VM_ENDPOINTS.get(region)
    if not vm_url:
        return jsonify({"error": f"no VM endpoint for region {region}"}), 404

    alert_id = alert["alert_id"]
    service = alert["service"]
    query = f'lab_alert_active{{alert_id="{alert_id}",service="{service}"}}'
    duration = request.args.get("duration", "1h")

    try:
        resp = requests.get(
            f"{vm_url}/api/v1/query_range",
            params={"query": query, "step": "15s",
                    "start": f"now-{duration}", "end": "now"},
            timeout=10,
        )
        return jsonify(resp.json())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502


# ---------------------------------------------------------------------------
# Log context (proxied from VictoriaLogs - lazy loaded on drill-down)
# ---------------------------------------------------------------------------

@app.route("/api/alerts/<int:alert_db_id>/logs", methods=["GET"])
def api_alert_logs(alert_db_id):
    """Fetch log lines for an alert from the regional VictoriaLogs."""
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM alerts WHERE id = %s", (alert_db_id,))
            alert = cur.fetchone()
    if not alert:
        return jsonify({"error": "not found"}), 404

    region = alert["region"]
    vl_url = VL_ENDPOINTS.get(region)
    if not vl_url:
        return jsonify({"error": f"no VL endpoint for region {region}"}), 404

    alert_id = alert["alert_id"]
    service = alert["service"]
    limit = request.args.get("limit", "100")
    duration = request.args.get("duration", "1h")

    # VictoriaLogs query
    log_query = f'{{service="{service}"}} AND "{alert_id}"'

    try:
        resp = requests.get(
            f"{vl_url}/select/logsql/query",
            params={"query": log_query, "limit": limit,
                    "start": f"now-{duration}", "end": "now"},
            timeout=10,
        )
        # VictoriaLogs returns newline-delimited JSON
        lines = []
        for line in resp.text.strip().split("\n"):
            if line:
                try:
                    lines.append(json.loads(line))
                except json.JSONDecodeError:
                    lines.append({"_msg": line})
        return jsonify(lines)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# Dashboard UI (single-page app served inline)
# ---------------------------------------------------------------------------

DASHBOARD_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Alert Dashboard</title>
<style>
:root {
  --bg: #0d1117; --surface: #161b22; --border: #30363d;
  --text: #e6edf3; --text-muted: #8b949e;
  --critical: #f85149; --warning: #d29922; --info: #58a6ff;
  --success: #3fb950; --btn: #21262d; --btn-hover: #30363d;
  --accent: #58a6ff;
}
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html { font-size: 14px; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
       background: var(--bg); color: var(--text); min-height: 100vh; }

/* --- Header --- */
.header { background: var(--surface); border-bottom: 1px solid var(--border);
           padding: .75rem 1.5rem; display: flex; align-items: center; gap: 1rem;
           position: sticky; top: 0; z-index: 100; }
.header h1 { font-size: 1.1rem; font-weight: 600; white-space: nowrap; }
.stat-pills { display: flex; gap: .5rem; flex-wrap: wrap; }
.pill { padding: .25rem .6rem; border-radius: 20px; font-size: .75rem; font-weight: 600; }
.pill-critical { background: rgba(248,81,73,.15); color: var(--critical); }
.pill-warning  { background: rgba(210,153,34,.15); color: var(--warning); }
.pill-info     { background: rgba(88,166,255,.15); color: var(--info); }
.pill-resolved { background: rgba(63,185,80,.15); color: var(--success); }
.header-right { margin-left: auto; display: flex; gap: .5rem; align-items: center; }

/* --- Filters --- */
.filters { padding: .5rem 1.5rem; background: var(--surface);
           border-bottom: 1px solid var(--border);
           display: flex; gap: .5rem; flex-wrap: wrap; align-items: center; }
.filter-btn { background: var(--btn); border: 1px solid var(--border); color: var(--text-muted);
              padding: .3rem .7rem; border-radius: 6px; font-size: .75rem; cursor: pointer; }
.filter-btn:hover { background: var(--btn-hover); color: var(--text); }
.filter-btn.active { border-color: var(--accent); color: var(--accent); }

/* --- Main layout --- */
.main { display: flex; height: calc(100vh - 90px); }
.alert-list { flex: 1; overflow-y: auto; padding: .75rem; min-width: 0; }
.detail-panel { width: 480px; min-width: 380px; background: var(--surface);
                border-left: 1px solid var(--border); overflow-y: auto;
                display: none; flex-direction: column; }
.detail-panel.open { display: flex; }

/* --- Alert cards --- */
.alert-card { background: var(--surface); border: 1px solid var(--border);
              border-radius: 8px; padding: .75rem 1rem; margin-bottom: .5rem;
              cursor: pointer; transition: border-color .15s; display: flex;
              align-items: flex-start; gap: .75rem; }
.alert-card:hover { border-color: var(--accent); }
.alert-card.selected { border-color: var(--accent); background: #1c2333; }
.alert-card.resolved { opacity: .55; }
.sev-indicator { width: 4px; border-radius: 2px; align-self: stretch; flex-shrink: 0; }
.sev-critical .sev-indicator { background: var(--critical); }
.sev-warning  .sev-indicator { background: var(--warning); }
.sev-info     .sev-indicator { background: var(--info); }
.alert-body { flex: 1; min-width: 0; }
.alert-title { font-weight: 600; font-size: .9rem; margin-bottom: .25rem;
               overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.alert-meta { font-size: .75rem; color: var(--text-muted); display: flex;
              flex-wrap: wrap; gap: .25rem .75rem; }
.badge { font-size: .7rem; padding: .15rem .45rem; border-radius: 4px; font-weight: 600; }
.badge-critical { background: rgba(248,81,73,.15); color: var(--critical); }
.badge-warning  { background: rgba(210,153,34,.15); color: var(--warning); }
.badge-info     { background: rgba(88,166,255,.15); color: var(--info); }
.badge-firing   { background: rgba(248,81,73,.15); color: var(--critical); }
.badge-resolved { background: rgba(63,185,80,.15); color: var(--success); }

/* --- Detail panel --- */
.detail-header { padding: 1rem; border-bottom: 1px solid var(--border); }
.detail-header h2 { font-size: 1rem; margin-bottom: .5rem; }
.detail-section { padding: 1rem; border-bottom: 1px solid var(--border); }
.detail-section h3 { font-size: .8rem; color: var(--text-muted); text-transform: uppercase;
                     letter-spacing: .05em; margin-bottom: .5rem; }
.detail-row { display: flex; justify-content: space-between; padding: .3rem 0;
              font-size: .8rem; }
.detail-row .label { color: var(--text-muted); }
.btn { padding: .4rem .8rem; border: none; border-radius: 6px; font-size: .8rem;
       font-weight: 500; cursor: pointer; transition: opacity .15s; }
.btn:hover { opacity: .85; }
.btn-close-alert { background: var(--success); color: #fff; }
.btn-close-alert:disabled { opacity: .4; cursor: default; }
.btn-refresh { background: var(--btn); border: 1px solid var(--border); color: var(--text); }
.btn-back { background: none; border: none; color: var(--accent); cursor: pointer;
            font-size: .8rem; padding: .25rem; display: none; }

/* --- Logs --- */
.log-entries { max-height: 400px; overflow-y: auto; }
.log-line { font-family: 'SF Mono', Monaco, 'Cascadia Code', monospace;
            font-size: .72rem; padding: .35rem .5rem; border-bottom: 1px solid var(--border);
            white-space: pre-wrap; word-break: break-all; line-height: 1.4; }
.log-line:nth-child(odd) { background: rgba(255,255,255,.02); }
.log-msg { color: var(--text); }
.log-time { color: var(--text-muted); margin-right: .5rem; }

/* --- Empty state --- */
.empty-state { text-align: center; padding: 3rem; color: var(--text-muted); }
.empty-state svg { margin-bottom: 1rem; opacity: .4; }

/* --- Spinner --- */
.spinner { display: inline-block; width: 16px; height: 16px;
           border: 2px solid var(--border); border-top-color: var(--accent);
           border-radius: 50%; animation: spin .6s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

/* --- Responsive --- */
@media (max-width: 900px) {
  .main { flex-direction: column; height: auto; }
  .detail-panel { width: 100%; min-width: 0; border-left: none; border-top: 1px solid var(--border); }
  .btn-back { display: inline-block; }
  .detail-panel.open ~ .alert-list { display: none; }
}
@media (max-width: 600px) {
  .header { flex-wrap: wrap; padding: .5rem .75rem; }
  .alert-list { padding: .5rem; }
  .filters { padding: .5rem .75rem; }
}
</style>
</head>
<body>

<div class="header">
  <h1>Alert Dashboard</h1>
  <div class="stat-pills" id="stat-pills"></div>
  <div class="header-right">
    <button class="btn btn-refresh" onclick="loadAlerts()">Refresh</button>
  </div>
</div>

<div class="filters" id="filters">
  <button class="filter-btn active" data-filter="status" data-value="">All</button>
  <button class="filter-btn" data-filter="status" data-value="firing">Firing</button>
  <button class="filter-btn" data-filter="status" data-value="resolved">Resolved</button>
  <span style="color:var(--border)">|</span>
  <button class="filter-btn active" data-filter="region" data-value="">All regions</button>
  <button class="filter-btn" data-filter="region" data-value="apac">APAC</button>
  <button class="filter-btn" data-filter="region" data-value="eu">EU</button>
  <button class="filter-btn" data-filter="region" data-value="us">US</button>
</div>

<div class="main">
  <div class="alert-list" id="alert-list">
    <div class="empty-state">Loading alerts&hellip;</div>
  </div>
  <div class="detail-panel" id="detail-panel"></div>
</div>

<script>
// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let alerts = [];
let selectedId = null;
let filters = { status: '', region: '' };
let autoRefreshTimer = null;

// ---------------------------------------------------------------------------
// API helpers
// ---------------------------------------------------------------------------
async function api(path, opts) {
  const resp = await fetch(path, opts);
  if (!resp.ok) throw new Error(`${resp.status}`);
  return resp.json();
}

// ---------------------------------------------------------------------------
// Load & render
// ---------------------------------------------------------------------------
async function loadAlerts() {
  try {
    const params = new URLSearchParams();
    if (filters.status) params.set('status', filters.status);
    if (filters.region) params.set('region', filters.region);
    alerts = await api('/api/alerts?' + params);
    renderAlertList();
    loadStats();
  } catch (e) {
    document.getElementById('alert-list').innerHTML =
      '<div class="empty-state">Failed to load alerts</div>';
  }
}

async function loadStats() {
  try {
    const s = await api('/api/alerts/stats');
    const el = document.getElementById('stat-pills');
    el.innerHTML = `
      <span class="pill pill-critical">${s.critical} critical</span>
      <span class="pill pill-warning">${s.warning} warning</span>
      <span class="pill pill-info">${s.info} info</span>
      <span class="pill pill-resolved">${s.resolved} resolved</span>
    `;
  } catch (_) {}
}

function renderAlertList() {
  const list = document.getElementById('alert-list');
  if (!alerts.length) {
    list.innerHTML = '<div class="empty-state">No alerts match the current filters</div>';
    return;
  }
  list.innerHTML = alerts.map(a => {
    const age = timeSince(a.starts_at || a.received_at);
    const sel = a.id === selectedId ? ' selected' : '';
    const res = a.status === 'resolved' ? ' resolved' : '';
    return `<div class="alert-card sev-${esc(a.severity)}${sel}${res}"
                 onclick="selectAlert(${a.id})">
      <div class="sev-indicator"></div>
      <div class="alert-body">
        <div class="alert-title">${esc(a.alert_name)}</div>
        <div class="alert-meta">
          <span class="badge badge-${esc(a.severity)}">${esc(a.severity)}</span>
          <span class="badge badge-${esc(a.status)}">${esc(a.status)}</span>
          <span>${esc(a.region)}/${esc(a.service)}</span>
          <span>${age}</span>
        </div>
        ${a.summary ? `<div style="font-size:.78rem;color:var(--text-muted);margin-top:.3rem">${esc(a.summary)}</div>` : ''}
      </div>
    </div>`;
  }).join('');
}

// ---------------------------------------------------------------------------
// Detail panel
// ---------------------------------------------------------------------------
async function selectAlert(id) {
  selectedId = id;
  renderAlertList();
  const panel = document.getElementById('detail-panel');
  panel.classList.add('open');
  panel.innerHTML = '<div style="padding:2rem;text-align:center"><div class="spinner"></div></div>';

  try {
    const a = await api(`/api/alerts/${id}`);
    const isFiring = a.status === 'firing';
    panel.innerHTML = `
      <div class="detail-header">
        <button class="btn-back" onclick="closePanel()">&larr; Back to list</button>
        <h2>${esc(a.alert_name)}</h2>
        <div style="display:flex;gap:.5rem;margin-top:.5rem;flex-wrap:wrap">
          <span class="badge badge-${esc(a.severity)}">${esc(a.severity)}</span>
          <span class="badge badge-${esc(a.status)}">${esc(a.status)}</span>
          ${isFiring ? `<button class="btn btn-close-alert" id="close-btn"
            onclick="closeAlert(${a.id})">Close Alert</button>` : ''}
        </div>
      </div>
      <div class="detail-section">
        <h3>Details</h3>
        <div class="detail-row"><span class="label">Region</span><span>${esc(a.region)}</span></div>
        <div class="detail-row"><span class="label">Service</span><span>${esc(a.service)}</span></div>
        <div class="detail-row"><span class="label">Instance</span><span>${esc(a.instance)}</span></div>
        <div class="detail-row"><span class="label">Alert ID</span><span style="font-family:monospace;font-size:.75rem">${esc(a.alert_id)}</span></div>
        <div class="detail-row"><span class="label">Started</span><span>${fmtTime(a.starts_at)}</span></div>
        ${a.resolved_at ? `<div class="detail-row"><span class="label">Resolved</span><span>${fmtTime(a.resolved_at)}</span></div>` : ''}
        ${a.resolved_by ? `<div class="detail-row"><span class="label">Resolved by</span><span>${esc(a.resolved_by)}</span></div>` : ''}
        ${a.summary ? `<div class="detail-row"><span class="label">Summary</span><span>${esc(a.summary)}</span></div>` : ''}
        ${a.description ? `<div class="detail-row"><span class="label">Description</span><span>${esc(a.description)}</span></div>` : ''}
      </div>
      <div class="detail-section">
        <h3>Labels</h3>
        ${Object.entries(a.labels || {}).map(([k,v]) =>
          `<div class="detail-row"><span class="label">${esc(k)}</span><span>${esc(String(v))}</span></div>`
        ).join('')}
      </div>
      <div class="detail-section">
        <h3>Logs <button class="btn btn-refresh" style="float:right;font-size:.7rem;padding:.2rem .5rem"
              onclick="loadLogs(${a.id})">Load logs</button></h3>
        <div id="log-container"><div style="color:var(--text-muted);font-size:.8rem">Click "Load logs" to fetch log context</div></div>
      </div>
      <div class="detail-section">
        <h3>History</h3>
        ${(a.history || []).map(h => `
          <div class="detail-row">
            <span class="badge badge-${esc(h.status)}">${esc(h.status)}</span>
            <span style="font-size:.75rem">${fmtTime(h.received_at)}</span>
          </div>
        `).join('')}
      </div>
    `;
  } catch (e) {
    panel.innerHTML = '<div style="padding:2rem;color:var(--critical)">Failed to load alert details</div>';
  }
}

function closePanel() {
  selectedId = null;
  document.getElementById('detail-panel').classList.remove('open');
  renderAlertList();
}

async function closeAlert(id) {
  const btn = document.getElementById('close-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Closing...'; }
  try {
    const result = await api(`/api/alerts/${id}/close`, { method: 'POST' });
    if (result.clear_warning) {
      console.warn('Upstream clear warning:', result.clear_warning);
    }
    await loadAlerts();
    selectAlert(id);
  } catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = 'Close Alert'; }
    alert('Failed to close alert: ' + e.message);
  }
}

// ---------------------------------------------------------------------------
// Logs (lazy loaded)
// ---------------------------------------------------------------------------
async function loadLogs(id) {
  const container = document.getElementById('log-container');
  container.innerHTML = '<div class="spinner"></div>';
  try {
    const logs = await api(`/api/alerts/${id}/logs`);
    if (!logs.length) {
      container.innerHTML = '<div style="color:var(--text-muted);font-size:.8rem">No log entries found</div>';
      return;
    }
    container.innerHTML = '<div class="log-entries">' + logs.map(l => {
      const time = l._time || '';
      const msg = l._msg || l.body || JSON.stringify(l);
      return `<div class="log-line"><span class="log-time">${esc(time)}</span><span class="log-msg">${esc(msg)}</span></div>`;
    }).join('') + '</div>';
  } catch (e) {
    container.innerHTML = `<div style="color:var(--critical);font-size:.8rem">Failed to load logs: ${esc(e.message)}</div>`;
  }
}

// ---------------------------------------------------------------------------
// Filters
// ---------------------------------------------------------------------------
document.getElementById('filters').addEventListener('click', e => {
  const btn = e.target.closest('.filter-btn');
  if (!btn) return;
  const group = btn.dataset.filter;
  const value = btn.dataset.value;
  filters[group] = value;
  // Update active states within the group
  document.querySelectorAll(`.filter-btn[data-filter="${group}"]`).forEach(b => {
    b.classList.toggle('active', b.dataset.value === value);
  });
  loadAlerts();
});

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function esc(s) {
  if (s == null) return '';
  const d = document.createElement('div');
  d.textContent = String(s);
  return d.innerHTML;
}

function timeSince(iso) {
  if (!iso) return '';
  const secs = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (secs < 60) return secs + 's ago';
  if (secs < 3600) return Math.floor(secs / 60) + 'm ago';
  if (secs < 86400) return Math.floor(secs / 3600) + 'h ago';
  return Math.floor(secs / 86400) + 'd ago';
}

function fmtTime(iso) {
  if (!iso) return '-';
  return new Date(iso).toLocaleString();
}

// ---------------------------------------------------------------------------
// Auto-refresh
// ---------------------------------------------------------------------------
function startAutoRefresh() {
  autoRefreshTimer = setInterval(loadAlerts, 10000);
}

// Init
loadAlerts();
startAutoRefresh();
</script>
</body>
</html>
"""


@app.route("/")
def dashboard():
    return Response(DASHBOARD_HTML, content_type="text/html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
