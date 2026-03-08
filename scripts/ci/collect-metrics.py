#!/usr/bin/env python3
"""Query VictoriaMetrics backends directly for round-trip latency metrics.

Queries each regional VM instance for lab_alert_roundtrip_seconds data
from the last 15 minutes and writes a consolidated results JSON file.
"""

import json
import os
import statistics
import sys
import time
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

OUTPUT_DIR = "perf-results"

# In Docker Compose, VM instances are on the lab-net network.
# From the host we query via Grafana's datasource proxy API to avoid
# needing extra port mappings.
GRAFANA_URL = "http://localhost:3000"
GRAFANA_DATASOURCES = {
    "apac": "victoriametrics-apac",
    "eu": "victoriametrics-eu",
    "us": "victoriametrics-us",
}


def query_vm(datasource_name: str, promql: str, start: float, end: float, step: str = "1s") -> list[dict]:
    """Query VictoriaMetrics via Grafana's datasource proxy."""
    # First, look up the datasource UID
    req = Request(f"{GRAFANA_URL}/api/datasources/name/{datasource_name}")
    try:
        with urlopen(req, timeout=10) as resp:
            ds = json.loads(resp.read())
    except (URLError, TimeoutError) as e:
        print(f"  Warning: could not look up datasource {datasource_name}: {e}", file=sys.stderr)
        return []

    ds_uid = ds["uid"]

    # Query via the datasource proxy
    params = urlencode({
        "query": promql,
        "start": int(start),
        "end": int(end),
        "step": step,
    })
    proxy_url = f"{GRAFANA_URL}/api/datasources/proxy/uid/{ds_uid}/api/v1/query_range?{params}"
    req = Request(proxy_url)
    try:
        with urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except (URLError, TimeoutError) as e:
        print(f"  Warning: query failed for {datasource_name}: {e}", file=sys.stderr)
        return []

    if data.get("status") != "success":
        print(f"  Warning: non-success response from {datasource_name}: {data.get('status')}", file=sys.stderr)
        return []

    return data.get("data", {}).get("result", [])


def extract_latency_values(results: list[dict]) -> list[float]:
    """Extract all non-zero latency values from query results."""
    values = []
    for series in results:
        for _ts, val in series.get("values", []):
            v = float(val)
            if v > 0:
                values.append(v)
    return values


def compute_stats(values: list[float]) -> dict:
    """Compute latency statistics from a list of values."""
    if not values:
        return {
            "count": 0,
            "min": None,
            "max": None,
            "mean": None,
            "median": None,
            "p90": None,
            "p95": None,
            "p99": None,
            "pct_under_5s": None,
        }

    sorted_vals = sorted(values)
    n = len(sorted_vals)
    under_5 = sum(1 for v in sorted_vals if v <= 5.0)

    return {
        "count": n,
        "min": round(sorted_vals[0], 3),
        "max": round(sorted_vals[-1], 3),
        "mean": round(statistics.mean(sorted_vals), 3),
        "median": round(statistics.median(sorted_vals), 3),
        "p90": round(sorted_vals[int(n * 0.9)], 3) if n > 1 else round(sorted_vals[0], 3),
        "p95": round(sorted_vals[int(n * 0.95)], 3) if n > 1 else round(sorted_vals[0], 3),
        "p99": round(sorted_vals[int(n * 0.99)], 3) if n > 1 else round(sorted_vals[0], 3),
        "pct_under_5s": round(100 * under_5 / n, 1),
    }


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    now = time.time()
    start = now - 900  # last 15 minutes

    all_values = []
    per_region = {}

    for region, ds_name in GRAFANA_DATASOURCES.items():
        print(f"Querying {region} ({ds_name})...")
        results = query_vm(ds_name, "lab_alert_roundtrip_seconds", start, now)
        values = extract_latency_values(results)
        print(f"  Found {len(values)} latency samples")
        per_region[region] = compute_stats(values)
        all_values.extend(values)

    overall = compute_stats(all_values)
    print(f"\nTotal samples: {overall['count']}")
    if overall["mean"] is not None:
        print(f"Overall mean: {overall['mean']:.3f}s")
        print(f"Overall p95:  {overall['p95']:.3f}s")
        print(f"Under 5s:     {overall['pct_under_5s']}%")

    output = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "overall": overall,
        "per_region": per_region,
    }

    output_path = os.path.join(OUTPUT_DIR, "results.json")
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nResults written to {output_path}")


if __name__ == "__main__":
    main()
