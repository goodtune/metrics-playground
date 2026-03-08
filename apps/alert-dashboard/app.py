"""Alert Dashboard - Operational alert management service.

Receives Alertmanager webhook notifications, stores alert state in PostgreSQL,
and serves a Datastar-powered operator dashboard. Connected browsers receive
real-time updates via SSE when webhooks arrive -- no polling needed.

Flask with threaded=True is sufficient for the lab's concurrency needs (a few
SSE connections alongside regular requests). For production scale, switch to
gunicorn with eventlet/gevent workers.
"""

import html as html_mod
import json
import os
import queue
import textwrap
import threading
import time
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
import requests as http_requests
from flask import Flask, Response, jsonify, request

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DB_DSN = os.environ.get(
    "DATABASE_URL",
    "postgresql://alertdash:alertdash@alert-dashboard-db:5432/alertdash",
)

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

SERVICE_ENDPOINTS = {}
for _region in ("apac", "eu", "us"):
    for _i in (1, 2, 3):
        _name = f"{_region}-app-{_i}"
        SERVICE_ENDPOINTS[_name] = f"http://{_name}:8080"

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
# SSE broadcast infrastructure
# ---------------------------------------------------------------------------

_subscribers: set[queue.Queue] = set()
_sub_lock = threading.Lock()


def _sse_event(*fragments: str) -> str:
    """Encode HTML fragments as Datastar SSE events (one per fragment)."""
    parts = []
    for frag in fragments:
        clean = " ".join(frag.split())
        parts.append(f"event: datastar-patch-elements\ndata: elements {clean}\n\n")
    return "".join(parts)


def _sse_response(*fragments: str) -> Response:
    """Build a one-shot SSE response for action endpoints."""
    return Response(
        _sse_event(*fragments),
        content_type="text/event-stream",
    )


def _broadcast():
    """Push the current alert list and stats to all connected viewers."""
    try:
        event = _sse_event(_render_alert_list(), _render_stats())
    except Exception as exc:
        print(f"[broadcast] render failed: {exc}", flush=True)
        return
    with _sub_lock:
        dead = set()
        for q in list(_subscribers):
            try:
                q.put_nowait(event)
            except queue.Full:
                dead.add(q)
        _subscribers -= dead


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

def _esc(s):
    """HTML-escape a value."""
    if s is None:
        return ""
    return html_mod.escape(str(s))


def _time_ago(val):
    """Format a datetime/ISO string as 'Xs ago'."""
    if not val:
        return ""
    try:
        if isinstance(val, str):
            dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
        else:
            dt = val
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        secs = max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
        if secs < 60:
            return f"{secs}s ago"
        if secs < 3600:
            return f"{secs // 60}m ago"
        if secs < 86400:
            return f"{secs // 3600}h ago"
        return f"{secs // 86400}d ago"
    except Exception:
        return ""


def _fmt_time(val):
    """Format a datetime/ISO string for display."""
    if not val:
        return "-"
    try:
        if isinstance(val, str):
            dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
        else:
            dt = val
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(val)[:19]


# ---------------------------------------------------------------------------
# Fragment rendering
# ---------------------------------------------------------------------------

def _fetch_all_alerts():
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""\
                SELECT id, alert_name, severity, region, service, instance,
                       alert_id, status, summary, starts_at, received_at
                FROM alerts
                ORDER BY
                    CASE status WHEN 'firing' THEN 0 ELSE 1 END,
                    CASE severity WHEN 'critical' THEN 0
                                  WHEN 'warning'  THEN 1 ELSE 2 END,
                    received_at DESC
            """)
            return cur.fetchall()


def _fetch_stats():
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
            return cur.fetchone()


def _render_alert_card(a):
    db_id = a["id"]
    status = _esc(a["status"])
    severity = _esc(a["severity"])
    region = _esc(a["region"])
    service = _esc(a["service"])
    name = _esc(a["alert_name"])
    summary = _esc(a.get("summary") or "")
    age = _time_ago(a["starts_at"] or a["received_at"])
    resolved_cls = " resolved" if a["status"] == "resolved" else ""

    show = (
        f"($filterStatus === '' || $filterStatus === '{status}') "
        f"&& ($filterRegion === '' || $filterRegion === '{region}')"
    )

    summary_html = ""
    if summary:
        summary_html = (
            f'<div style="font-size:.78rem;color:var(--text-muted);'
            f'margin-top:.3rem">{summary}</div>'
        )

    return (
        f'<div class="alert-card sev-{severity}{resolved_cls}" '
        f'data-show="{show}" '
        f'data-class-selected="$selectedId === {db_id}" '
        f"data-on:click=\"$selectedId = {db_id}; @post('/alerts/select')\">"
        f'<div class="sev-indicator"></div>'
        f'<div class="alert-body">'
        f'<div class="alert-title">{name}</div>'
        f'<div class="alert-meta">'
        f'<span class="badge badge-{severity}">{severity}</span>'
        f'<span class="badge badge-{status}">{status}</span>'
        f'<span>{region}/{service}</span>'
        f'<span>{age}</span>'
        f'</div>'
        f'{summary_html}'
        f'</div>'
        f'</div>'
    )


def _render_alert_list():
    alerts = _fetch_all_alerts()
    if not alerts:
        return (
            '<div class="alert-list" id="alert-list">'
            '<div class="empty-state">No alerts</div>'
            '</div>'
        )
    cards = "".join(_render_alert_card(a) for a in alerts)
    return f'<div class="alert-list" id="alert-list">{cards}</div>'


def _render_stats():
    s = _fetch_stats()
    return (
        f'<div class="stat-pills" id="stat-pills">'
        f'<span class="pill pill-critical">{s["critical"]} critical</span>'
        f'<span class="pill pill-warning">{s["warning"]} warning</span>'
        f'<span class="pill pill-info">{s["info"]} info</span>'
        f'<span class="pill pill-resolved">{s["resolved"]} resolved</span>'
        f'</div>'
    )


def _render_detail(alert_db_id):
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM alerts WHERE id = %s", (alert_db_id,))
            a = cur.fetchone()
            if not a:
                return (
                    '<div id="detail-content">'
                    '<div class="empty-state">Alert not found</div>'
                    '</div>'
                )
            cur.execute(
                "SELECT * FROM alert_history WHERE alert_id = %s ORDER BY received_at",
                (alert_db_id,),
            )
            history = cur.fetchall()

    db_id = a["id"]
    is_firing = a["status"] == "firing"

    # Labels
    labels = a.get("labels") or {}
    if isinstance(labels, str):
        try:
            labels = json.loads(labels)
        except Exception:
            labels = {}
    labels_html = "".join(
        f'<div class="detail-row"><span class="label">{_esc(k)}</span>'
        f'<span>{_esc(str(v))}</span></div>'
        for k, v in labels.items()
    )

    # History
    history_html = "".join(
        f'<div class="detail-row">'
        f'<span class="badge badge-{_esc(h["status"])}">{_esc(h["status"])}</span>'
        f'<span style="font-size:.75rem">{_fmt_time(h["received_at"])}</span>'
        f'</div>'
        for h in history
    )

    close_btn = ""
    if is_firing:
        close_btn = (
            f'<button class="btn btn-close-alert" '
            f"data-on:click=\"@post('/alerts/{db_id}/close')\">"
            f'Close Alert</button>'
        )

    resolved_row = ""
    if a["resolved_at"]:
        resolved_row = (
            f'<div class="detail-row"><span class="label">Resolved</span>'
            f'<span>{_fmt_time(a["resolved_at"])}</span></div>'
        )

    resolved_by_row = ""
    if a.get("resolved_by"):
        resolved_by_row = (
            f'<div class="detail-row"><span class="label">Resolved by</span>'
            f'<span>{_esc(a["resolved_by"])}</span></div>'
        )

    summary_row = ""
    if a.get("summary"):
        summary_row = (
            f'<div class="detail-row"><span class="label">Summary</span>'
            f'<span>{_esc(a["summary"])}</span></div>'
        )

    return (
        f'<div id="detail-content">'
        f'<div class="detail-header">'
        f'<button class="btn-back" data-on:click="$selectedId = 0">'
        f'&larr; Back to list</button>'
        f'<h2>{_esc(a["alert_name"])}</h2>'
        f'<div style="display:flex;gap:.5rem;margin-top:.5rem;flex-wrap:wrap">'
        f'<span class="badge badge-{_esc(a["severity"])}">{_esc(a["severity"])}</span>'
        f'<span class="badge badge-{_esc(a["status"])}">{_esc(a["status"])}</span>'
        f'{close_btn}'
        f'</div>'
        f'</div>'
        f'<div class="detail-section">'
        f'<h3>Details</h3>'
        f'<div class="detail-row"><span class="label">Region</span>'
        f'<span>{_esc(a["region"])}</span></div>'
        f'<div class="detail-row"><span class="label">Service</span>'
        f'<span>{_esc(a["service"])}</span></div>'
        f'<div class="detail-row"><span class="label">Instance</span>'
        f'<span>{_esc(a["instance"])}</span></div>'
        f'<div class="detail-row"><span class="label">Alert ID</span>'
        f'<span style="font-family:monospace;font-size:.75rem">'
        f'{_esc(a["alert_id"])}</span></div>'
        f'<div class="detail-row"><span class="label">Started</span>'
        f'<span>{_fmt_time(a["starts_at"])}</span></div>'
        f'{resolved_row}'
        f'{resolved_by_row}'
        f'{summary_row}'
        f'</div>'
        f'<div class="detail-section">'
        f'<h3>Labels</h3>'
        f'{labels_html}'
        f'</div>'
        f'<div class="detail-section">'
        f'<h3>Logs '
        f'<button class="btn btn-refresh" '
        f'style="float:right;font-size:.7rem;padding:.2rem .5rem" '
        f"data-on:click=\"@get('/alerts/{db_id}/load-logs')\">"
        f'Load logs</button></h3>'
        f'<div id="log-container">'
        f'<div style="color:var(--text-muted);font-size:.8rem">'
        f'Click "Load logs" to fetch log context</div>'
        f'</div>'
        f'</div>'
        f'<div class="detail-section">'
        f'<h3>History</h3>'
        f'{history_html}'
        f'</div>'
        f'</div>'
    )


def _render_logs(alert_db_id):
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM alerts WHERE id = %s", (alert_db_id,))
            a = cur.fetchone()
    if not a:
        return (
            '<div id="log-container">'
            '<div style="color:var(--critical);font-size:.8rem">Alert not found</div>'
            '</div>'
        )

    region = a["region"]
    vl_url = VL_ENDPOINTS.get(region)
    if not vl_url:
        return (
            '<div id="log-container">'
            f'<div style="color:var(--critical);font-size:.8rem">'
            f'No log endpoint for region {_esc(region)}</div>'
            '</div>'
        )

    alert_id_val = a["alert_id"]
    # Full-text search by alert_id (a UUID, so virtually no false positives).
    # More reliable than stream-label filters for OTLP-ingested logs.
    log_query = f'"{alert_id_val}"'

    try:
        resp = http_requests.get(
            f"{vl_url}/select/logsql/query",
            params={"query": log_query, "limit": "100",
                    "start": "now-1h", "end": "now"},
            timeout=10,
        )
        lines = []
        for line in resp.text.strip().split("\n"):
            if line:
                try:
                    lines.append(json.loads(line))
                except json.JSONDecodeError:
                    lines.append({"_msg": line})

        if not lines:
            return (
                '<div id="log-container">'
                '<div style="color:var(--text-muted);font-size:.8rem">'
                'No log entries found</div>'
                '</div>'
            )

        entries = "".join(
            f'<div class="log-line">'
            f'<span class="log-time">{_esc(l.get("_time", ""))}</span>'
            f'<span class="log-msg">'
            f'{_esc(l.get("_msg") or l.get("body") or json.dumps(l))}</span>'
            f'</div>'
            for l in lines
        )
        return f'<div id="log-container"><div class="log-entries">{entries}</div></div>'

    except Exception as exc:
        return (
            '<div id="log-container">'
            f'<div style="color:var(--critical);font-size:.8rem">'
            f'Failed to load logs: {_esc(str(exc))}</div>'
            '</div>'
        )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/webhook", methods=["POST"])
def alertmanager_webhook():
    """Receive Alertmanager webhook payload, upsert, and broadcast to viewers."""
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
                alert_id_val = labels.get("alert_id", "")
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
                        instance, alert_id_val, status, summary, description,
                        json.dumps(labels), json.dumps(annotations),
                        starts_at, ends_at,
                    ),
                )
                row = cur.fetchone()
                if row:
                    cur.execute(
                        "INSERT INTO alert_history (alert_id, status, raw_payload) "
                        "VALUES (%s, %s, %s)",
                        (row[0], status, json.dumps(alert)),
                    )
                processed += 1
        conn.commit()

    _broadcast()
    return jsonify({"processed": processed})


@app.route("/feed")
def feed():
    """Long-lived SSE stream. Pushes alert list and stats on every webhook."""
    def generate():
        q = queue.Queue(maxsize=50)
        with _sub_lock:
            _subscribers.add(q)
        try:
            yield _sse_event(_render_alert_list(), _render_stats())
            while True:
                try:
                    event = q.get(timeout=15)
                    yield event
                except queue.Empty:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            pass
        finally:
            with _sub_lock:
                _subscribers.discard(q)

    return Response(
        generate(),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/alerts/select", methods=["POST"])
def select_alert():
    """Return detail panel fragment for the selected alert."""
    data = request.get_json(force=True, silent=True) or {}
    alert_db_id = data.get("selectedId", 0)
    if not alert_db_id:
        return _sse_response('<div id="detail-content"></div>')
    return _sse_response(_render_detail(int(alert_db_id)))


@app.route("/alerts/<int:alert_db_id>/close", methods=["POST"])
def close_alert(alert_db_id):
    """Operator closes an alert. Pushes clear to originating app, broadcasts."""
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM alerts WHERE id = %s", (alert_db_id,))
            alert = cur.fetchone()
            if not alert:
                return _sse_response(
                    '<div id="detail-content">'
                    '<div class="empty-state">Alert not found</div></div>'
                )
            if alert["status"] == "resolved":
                return _sse_response(_render_detail(alert_db_id))

            # Best-effort push of synthetic clear to the originating workload
            service = alert["service"]
            alert_id_val = alert["alert_id"]
            endpoint = SERVICE_ENDPOINTS.get(service)
            if endpoint and alert_id_val:
                try:
                    http_requests.post(
                        f"{endpoint}/clear",
                        json={"alert_id": alert_id_val,
                              "reason": "operator-dashboard-close"},
                        timeout=5,
                    )
                except Exception:
                    pass

            cur.execute(
                "UPDATE alerts SET status = 'resolved', resolved_at = NOW(), "
                "resolved_by = 'operator' WHERE id = %s",
                (alert_db_id,),
            )
            cur.execute(
                "INSERT INTO alert_history (alert_id, status) VALUES (%s, 'resolved')",
                (alert_db_id,),
            )
        conn.commit()

    _broadcast()
    return _sse_response(_render_detail(alert_db_id))


@app.route("/alerts/<int:alert_db_id>/load-logs")
def load_logs(alert_db_id):
    """Lazy-load log entries for an alert from regional VictoriaLogs."""
    return _sse_response(_render_logs(alert_db_id))


# JSON API (kept for programmatic / test access)

@app.route("/api/alerts", methods=["GET"])
def api_list_alerts():
    status = request.args.get("status")
    region = request.args.get("region")
    clauses, params = [], []
    if status:
        clauses.append("status = %s")
        params.append(status)
    if region:
        clauses.append("region = %s")
        params.append(region)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"SELECT * FROM alerts {where} ORDER BY received_at DESC",
                params,
            )
            rows = cur.fetchall()
    for row in rows:
        for k in ("starts_at", "ends_at", "received_at", "resolved_at"):
            if row.get(k) is not None:
                row[k] = row[k].isoformat()
    return jsonify(rows)


@app.route("/api/alerts/stats", methods=["GET"])
def api_alert_stats():
    return jsonify(_fetch_stats())


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# Dashboard UI (Datastar-powered, SSE-driven)
# ---------------------------------------------------------------------------

DASHBOARD_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Alert Dashboard</title>
<script type="module" src="https://cdn.jsdelivr.net/gh/starfederation/datastar@v1.0.0-RC.8/bundles/datastar.js"></script>
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

.filters { padding: .5rem 1.5rem; background: var(--surface);
           border-bottom: 1px solid var(--border);
           display: flex; gap: .5rem; flex-wrap: wrap; align-items: center; }
.filter-btn { background: var(--btn); border: 1px solid var(--border); color: var(--text-muted);
              padding: .3rem .7rem; border-radius: 6px; font-size: .75rem; cursor: pointer; }
.filter-btn:hover { background: var(--btn-hover); color: var(--text); }
.filter-btn.active { border-color: var(--accent); color: var(--accent); }

.main { display: flex; height: calc(100vh - 90px); }
.alert-list { flex: 1; overflow-y: auto; padding: .75rem; min-width: 0; }
.detail-panel { width: 480px; min-width: 380px; background: var(--surface);
                border-left: 1px solid var(--border); overflow-y: auto;
                display: flex; flex-direction: column; }

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

.detail-header { padding: 1rem; border-bottom: 1px solid var(--border); }
.detail-header h2 { font-size: 1rem; margin-bottom: .5rem; }
.detail-section { padding: 1rem; border-bottom: 1px solid var(--border); }
.detail-section h3 { font-size: .8rem; color: var(--text-muted); text-transform: uppercase;
                     letter-spacing: .05em; margin-bottom: .5rem; }
.detail-row { display: flex; justify-content: space-between; padding: .3rem 0; font-size: .8rem; }
.detail-row .label { color: var(--text-muted); }
.btn { padding: .4rem .8rem; border: none; border-radius: 6px; font-size: .8rem;
       font-weight: 500; cursor: pointer; transition: opacity .15s; }
.btn:hover { opacity: .85; }
.btn-close-alert { background: var(--success); color: #fff; }
.btn-back { background: none; border: none; color: var(--accent); cursor: pointer;
            font-size: .8rem; padding: .25rem; }
.btn-refresh { background: var(--btn); border: 1px solid var(--border); color: var(--text); }

.log-entries { max-height: 400px; overflow-y: auto; }
.log-line { font-family: 'SF Mono', Monaco, 'Cascadia Code', monospace;
            font-size: .72rem; padding: .35rem .5rem; border-bottom: 1px solid var(--border);
            white-space: pre-wrap; word-break: break-all; line-height: 1.4; }
.log-line:nth-child(odd) { background: rgba(255,255,255,.02); }
.log-msg { color: var(--text); }
.log-time { color: var(--text-muted); margin-right: .5rem; }

.empty-state { text-align: center; padding: 3rem; color: var(--text-muted); }
.spinner { display: inline-block; width: 16px; height: 16px;
           border: 2px solid var(--border); border-top-color: var(--accent);
           border-radius: 50%; animation: spin .6s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

@media (max-width: 900px) {
  .main { flex-direction: column; height: auto; }
  .detail-panel { width: 100%; min-width: 0; border-left: none;
                  border-top: 1px solid var(--border); }
}
</style>
</head>
<body data-signals="{filterStatus: '', filterRegion: '', selectedId: 0}">

<div class="header">
  <h1>Alert Dashboard</h1>
  <div class="stat-pills" id="stat-pills"></div>
</div>

<div class="filters">
  <button class="filter-btn" data-class-active="$filterStatus === ''"
          data-on:click="$filterStatus = ''">All</button>
  <button class="filter-btn" data-class-active="$filterStatus === 'firing'"
          data-on:click="$filterStatus = 'firing'">Firing</button>
  <button class="filter-btn" data-class-active="$filterStatus === 'resolved'"
          data-on:click="$filterStatus = 'resolved'">Resolved</button>
  <span style="color:var(--border)">|</span>
  <button class="filter-btn" data-class-active="$filterRegion === ''"
          data-on:click="$filterRegion = ''">All regions</button>
  <button class="filter-btn" data-class-active="$filterRegion === 'apac'"
          data-on:click="$filterRegion = 'apac'">APAC</button>
  <button class="filter-btn" data-class-active="$filterRegion === 'eu'"
          data-on:click="$filterRegion = 'eu'">EU</button>
  <button class="filter-btn" data-class-active="$filterRegion === 'us'"
          data-on:click="$filterRegion = 'us'">US</button>
</div>

<div class="main" data-on:load="@get('/feed')">
  <div class="alert-list" id="alert-list">
    <div class="empty-state">Connecting to live feed&hellip;</div>
  </div>
  <div class="detail-panel" id="detail-panel" data-show="$selectedId > 0">
    <div id="detail-content"></div>
  </div>
</div>

</body>
</html>
"""


@app.route("/")
def dashboard():
    return Response(DASHBOARD_HTML, content_type="text/html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, threaded=True)
