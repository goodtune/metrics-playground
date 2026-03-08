#!/usr/bin/env python3
"""Fetch the last 3 successful performance results from the main branch.

Used on pull requests to establish a baseline for regression detection.
Downloads artifacts from the 3 most recent successful runs and computes
aggregate statistics.
"""

import json
import os
import subprocess
import sys

OUTPUT_DIR = "perf-results"
WORKFLOW_NAME = "performance.yml"
BASELINE_COUNT = 3


def run_gh(*args: str) -> str:
    """Run a gh CLI command and return stdout."""
    result = subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Find recent successful runs on main
    runs_json = run_gh(
        "run", "list",
        "--workflow", WORKFLOW_NAME,
        "--branch", "main",
        "--status", "success",
        "--limit", str(BASELINE_COUNT),
        "--json", "databaseId,createdAt",
    )

    if not runs_json:
        print("No successful main branch runs found. Skipping baseline.")
        return

    runs = json.loads(runs_json)
    if not runs:
        print("No successful main branch runs found. Skipping baseline.")
        return

    print(f"Found {len(runs)} successful main branch run(s)")

    baseline_results = []
    for run in runs:
        run_id = str(run["databaseId"])
        run_dir = os.path.join(OUTPUT_DIR, f"baseline-{run_id}")
        os.makedirs(run_dir, exist_ok=True)

        result = subprocess.run(
            ["gh", "run", "download", run_id,
             "--name", "performance-results",
             "--dir", run_dir],
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            print(f"  Could not download run {run_id}: {result.stderr}")
            continue

        results_path = os.path.join(run_dir, "results.json")
        if os.path.exists(results_path):
            with open(results_path) as f:
                data = json.load(f)
            baseline_results.append(data)
            overall = data.get("overall", {})
            print(f"  Run {run_id}: mean={overall.get('mean')}s, p95={overall.get('p95')}s")
        else:
            print(f"  Run {run_id}: results.json not found in artifact")

    if not baseline_results:
        print("No baseline data could be loaded.")
        return

    # Compute aggregate baseline
    means = [r["overall"]["mean"] for r in baseline_results if r["overall"].get("mean") is not None]
    p95s = [r["overall"]["p95"] for r in baseline_results if r["overall"].get("p95") is not None]
    p99s = [r["overall"]["p99"] for r in baseline_results if r["overall"].get("p99") is not None]
    pcts = [r["overall"]["pct_under_5s"] for r in baseline_results if r["overall"].get("pct_under_5s") is not None]

    baseline = {
        "run_count": len(baseline_results),
        "avg_mean": round(sum(means) / len(means), 3) if means else None,
        "avg_p95": round(sum(p95s) / len(p95s), 3) if p95s else None,
        "avg_p99": round(sum(p99s) / len(p99s), 3) if p99s else None,
        "avg_pct_under_5s": round(sum(pcts) / len(pcts), 1) if pcts else None,
        "individual_runs": [
            {
                "timestamp": r.get("timestamp"),
                "mean": r["overall"].get("mean"),
                "p95": r["overall"].get("p95"),
                "p99": r["overall"].get("p99"),
                "pct_under_5s": r["overall"].get("pct_under_5s"),
            }
            for r in baseline_results
        ],
    }

    baseline_path = os.path.join(OUTPUT_DIR, "baseline.json")
    with open(baseline_path, "w") as f:
        json.dump(baseline, f, indent=2)
    print(f"\nBaseline written to {baseline_path}")
    print(f"  Avg mean:       {baseline['avg_mean']}s")
    print(f"  Avg p95:        {baseline['avg_p95']}s")
    print(f"  Avg under 5s:   {baseline['avg_pct_under_5s']}%")


if __name__ == "__main__":
    main()
