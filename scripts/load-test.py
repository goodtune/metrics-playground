#!/usr/bin/env python3
"""Alert pipeline load test.

Raises and clears alerts across all 9 app instances over 10 minutes
with varying patterns (bursts, sporadic, steady) to validate consistent
~5s round-trip latency.
"""

import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.error import URLError
from urllib.request import Request, urlopen
import json

APPS = [
    {"name": "apac-app-1", "port": 8081, "region": "apac"},
    {"name": "apac-app-2", "port": 8082, "region": "apac"},
    {"name": "apac-app-3", "port": 8083, "region": "apac"},
    {"name": "eu-app-1",   "port": 8084, "region": "eu"},
    {"name": "eu-app-2",   "port": 8085, "region": "eu"},
    {"name": "eu-app-3",   "port": 8086, "region": "eu"},
    {"name": "us-app-1",   "port": 8087, "region": "us"},
    {"name": "us-app-2",   "port": 8088, "region": "us"},
    {"name": "us-app-3",   "port": 8089, "region": "us"},
]

ALERT_NAMES = [
    "HighLatency", "DiskPressure", "MemoryExhausted", "CPUThrottle",
    "ConnectionPoolFull", "QueueBacklog", "CertExpiring", "ErrorRateSpike",
    "SlowQueries", "ReplicationLag",
]

SEVERITIES = ["critical", "warning", "info"]

DURATION = 600  # 10 minutes
MAX_ACTIVE_PER_APP = 5  # clear before raising more than this

# Track active alerts per app for lifecycle management
active: dict[int, list[str]] = {app["port"]: [] for app in APPS}


def api_call(port: int, method: str, path: str, body: dict | None = None) -> dict | None:
    url = f"http://localhost:{port}{path}"
    data = json.dumps(body).encode() if body else None
    req = Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except (URLError, TimeoutError, json.JSONDecodeError):
        return None


def raise_alert(app: dict) -> str | None:
    port = app["port"]
    name = random.choice(ALERT_NAMES)
    severity = random.choice(SEVERITIES)
    result = api_call(port, "POST", "/raise", {
        "alert_name": name,
        "severity": severity,
        "message": f"load-test on {app['name']}",
    })
    if result and "alert_id" in result:
        active[port].append(result["alert_id"])
        return f"  RAISE {app['name']:12s} {name:20s} {severity:8s} -> {result['alert_id'][:8]}"
    return None


def clear_alert(app: dict, alert_id: str) -> str | None:
    port = app["port"]
    result = api_call(port, "POST", "/clear", {"alert_id": alert_id})
    if result:
        if alert_id in active[port]:
            active[port].remove(alert_id)
        return f"  CLEAR {app['name']:12s} {alert_id[:8]}"
    return None


def clear_oldest(app: dict, count: int = 1) -> list[str]:
    port = app["port"]
    msgs = []
    for _ in range(min(count, len(active[port]))):
        alert_id = active[port][0]
        msg = clear_alert(app, alert_id)
        if msg:
            msgs.append(msg)
    return msgs


def burst_phase(executor: ThreadPoolExecutor) -> list[str]:
    """Raise alerts on many apps simultaneously."""
    count = random.randint(3, 9)
    targets = random.sample(APPS, count)
    msgs = [f"  BURST: {count} alerts across {len(set(a['region'] for a in targets))} regions"]
    futures = {executor.submit(raise_alert, app): app for app in targets}
    for f in as_completed(futures):
        msg = f.result()
        if msg:
            msgs.append(msg)
    return msgs


def sporadic_phase(executor: ThreadPoolExecutor) -> list[str]:
    """Raise 1-2 alerts on random apps."""
    count = random.randint(1, 2)
    targets = random.sample(APPS, count)
    msgs = []
    for app in targets:
        msg = raise_alert(app)
        if msg:
            msgs.append(msg)
    return msgs


def steady_phase(executor: ThreadPoolExecutor) -> list[str]:
    """Raise one alert per region."""
    msgs = [f"  STEADY: one per region"]
    regions = {"apac": [], "eu": [], "us": []}
    for app in APPS:
        regions[app["region"]].append(app)
    targets = [random.choice(apps) for apps in regions.values()]
    futures = {executor.submit(raise_alert, app): app for app in targets}
    for f in as_completed(futures):
        msg = f.result()
        if msg:
            msgs.append(msg)
    return msgs


def cleanup_phase() -> list[str]:
    """Clear alerts on apps that have too many active."""
    msgs = []
    for app in APPS:
        port = app["port"]
        if len(active[port]) > MAX_ACTIVE_PER_APP:
            excess = len(active[port]) - MAX_ACTIVE_PER_APP + 2
            msgs.extend(clear_oldest(app, excess))
    return msgs


def clear_all() -> None:
    for app in APPS:
        api_call(app["port"], "POST", "/alerts/clear-all")
        active[app["port"]].clear()


def main():
    duration = int(sys.argv[1]) if len(sys.argv) > 1 else DURATION
    print(f"Alert pipeline load test - {duration}s across {len(APPS)} apps")
    print(f"{'=' * 60}")

    # Start clean
    print("Clearing all existing alerts...")
    clear_all()
    time.sleep(2)

    start = time.time()
    cycle = 0
    executor = ThreadPoolExecutor(max_workers=9)

    phases = [burst_phase, sporadic_phase, sporadic_phase, steady_phase]
    weights = [2, 4, 4, 3]  # sporadic most common

    try:
        while time.time() - start < duration:
            cycle += 1
            elapsed = time.time() - start
            remaining = duration - elapsed
            total_active = sum(len(v) for v in active.values())

            print(f"\n[{elapsed:6.0f}s / {duration}s] cycle {cycle}  "
                  f"active={total_active}  remaining={remaining:.0f}s")

            # Pick a phase
            phase = random.choices(phases, weights=weights, k=1)[0]
            msgs = phase(executor)
            for m in msgs:
                print(m)

            # Periodic cleanup
            if cycle % 3 == 0:
                msgs = cleanup_phase()
                if msgs:
                    print(f"  CLEANUP:")
                    for m in msgs:
                        print(m)

            # Occasional bulk clear to reset
            if cycle % 20 == 0:
                app = random.choice(APPS)
                api_call(app["port"], "POST", "/alerts/clear-all")
                active[app["port"]].clear()
                print(f"  RESET {app['name']}")

            # Variable sleep between cycles
            if phase == burst_phase:
                delay = random.uniform(8, 15)
            elif phase == steady_phase:
                delay = random.uniform(5, 10)
            else:
                delay = random.uniform(3, 8)

            # Don't overshoot
            if time.time() - start + delay > duration:
                break
            time.sleep(delay)

    except KeyboardInterrupt:
        print("\n\nInterrupted.")
    finally:
        executor.shutdown(wait=False)
        total_active = sum(len(v) for v in active.values())
        elapsed = time.time() - start
        print(f"\n{'=' * 60}")
        print(f"Completed: {cycle} cycles in {elapsed:.0f}s, {total_active} alerts still active")
        print(f"Check Grafana at http://localhost:3000 for round-trip latency data")


if __name__ == "__main__":
    main()
