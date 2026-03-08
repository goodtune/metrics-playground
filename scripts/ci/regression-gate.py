#!/usr/bin/env python3
"""Regression gate for pull requests.

Compares the current run's latency metrics against the baseline from the
last 3 successful main branch runs. Fails if the PR's performance strays
too far from the main branch averages.

Thresholds:
  - Mean latency: must not exceed baseline mean by more than 50%
  - P95 latency:  must not exceed baseline P95 by more than 50%
  - Under-5s %:   must not drop below baseline by more than 15 percentage points
"""

import json
import os
import sys

OUTPUT_DIR = "perf-results"

# How much worse than baseline is tolerable
MEAN_THRESHOLD_FACTOR = 1.5  # 50% above baseline mean
P95_THRESHOLD_FACTOR = 1.5   # 50% above baseline P95
PCT_UNDER_5S_DROP = 15.0     # percentage points below baseline


def load_json(path: str) -> dict | None:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def main():
    results = load_json(os.path.join(OUTPUT_DIR, "results.json"))
    baseline = load_json(os.path.join(OUTPUT_DIR, "baseline.json"))

    if results is None:
        print("ERROR: No current results found.")
        sys.exit(1)

    if baseline is None:
        print("No baseline data available — skipping regression check.")
        print("This is expected for the first run on main.")
        sys.exit(0)

    overall = results["overall"]
    failures = []

    # Check mean latency
    if baseline.get("avg_mean") is not None and overall.get("mean") is not None:
        threshold = baseline["avg_mean"] * MEAN_THRESHOLD_FACTOR
        if overall["mean"] > threshold:
            failures.append(
                f"Mean latency {overall['mean']:.3f}s exceeds threshold "
                f"{threshold:.3f}s (baseline {baseline['avg_mean']:.3f}s x {MEAN_THRESHOLD_FACTOR})"
            )
        else:
            print(f"PASS: Mean latency {overall['mean']:.3f}s <= {threshold:.3f}s threshold")

    # Check P95 latency
    if baseline.get("avg_p95") is not None and overall.get("p95") is not None:
        threshold = baseline["avg_p95"] * P95_THRESHOLD_FACTOR
        if overall["p95"] > threshold:
            failures.append(
                f"P95 latency {overall['p95']:.3f}s exceeds threshold "
                f"{threshold:.3f}s (baseline {baseline['avg_p95']:.3f}s x {P95_THRESHOLD_FACTOR})"
            )
        else:
            print(f"PASS: P95 latency {overall['p95']:.3f}s <= {threshold:.3f}s threshold")

    # Check percentage under 5s
    if baseline.get("avg_pct_under_5s") is not None and overall.get("pct_under_5s") is not None:
        min_acceptable = baseline["avg_pct_under_5s"] - PCT_UNDER_5S_DROP
        if overall["pct_under_5s"] < min_acceptable:
            failures.append(
                f"Under-5s rate {overall['pct_under_5s']:.1f}% is below minimum "
                f"{min_acceptable:.1f}% (baseline {baseline['avg_pct_under_5s']:.1f}% - {PCT_UNDER_5S_DROP}pp)"
            )
        else:
            print(f"PASS: Under-5s rate {overall['pct_under_5s']:.1f}% >= {min_acceptable:.1f}% minimum")

    if failures:
        print()
        print("REGRESSION DETECTED:")
        for f in failures:
            print(f"  FAIL: {f}")
        print()
        print("This PR's performance has regressed significantly compared to the")
        print("last 3 successful runs on main. Please investigate before merging.")
        sys.exit(1)
    else:
        print()
        print("All regression checks passed.")


if __name__ == "__main__":
    main()
