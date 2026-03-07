#!/usr/bin/env python3
"""Generate a GitHub Actions job summary with performance results.

Reads the current run's results, any previous run data, and dashboard
screenshots. Outputs GitHub-flavored Markdown to stdout for use with
$GITHUB_STEP_SUMMARY.

Dashboard screenshots are embedded as data URIs so they appear inline
in the job summary without needing external hosting.
"""

import base64
import json
import os
import sys

OUTPUT_DIR = ".perf-results"


def load_json(path: str) -> dict | None:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def image_to_data_uri(path: str) -> str | None:
    """Convert a PNG file to a data URI string."""
    try:
        with open(path, "rb") as f:
            data = base64.b64encode(f.read()).decode("ascii")
        return f"data:image/png;base64,{data}"
    except FileNotFoundError:
        return None


def format_val(val, suffix="s") -> str:
    if val is None:
        return "N/A"
    return f"{val}{suffix}"


def main():
    results = load_json(os.path.join(OUTPUT_DIR, "results.json"))
    previous = load_json(os.path.join(OUTPUT_DIR, "previous", "results.json"))
    baseline = load_json(os.path.join(OUTPUT_DIR, "baseline.json"))

    print("# Alert Pipeline Performance Report")
    print()

    if results is None:
        print("> **Error**: No results found. The metrics collection step may have failed.")
        return

    overall = results["overall"]
    ts = results.get("timestamp", "unknown")
    print(f"**Run timestamp**: {ts}")
    print()

    # --- Current results table ---
    print("## End-to-End Latency")
    print()
    print("| Metric | Value |")
    print("|--------|-------|")
    print(f"| Samples | {overall['count']} |")
    print(f"| Min | {format_val(overall['min'])} |")
    print(f"| Mean | {format_val(overall['mean'])} |")
    print(f"| Median | {format_val(overall['median'])} |")
    print(f"| P90 | {format_val(overall['p90'])} |")
    print(f"| P95 | {format_val(overall['p95'])} |")
    print(f"| P99 | {format_val(overall['p99'])} |")
    print(f"| Max | {format_val(overall['max'])} |")
    print(f"| Under 5s | {format_val(overall['pct_under_5s'], '%')} |")
    print()

    # --- Per-region breakdown ---
    per_region = results.get("per_region", {})
    if per_region:
        print("### Per-Region Breakdown")
        print()
        print("| Region | Samples | Mean | P95 | P99 | Under 5s |")
        print("|--------|---------|------|-----|-----|----------|")
        for region in ["apac", "eu", "us"]:
            r = per_region.get(region, {})
            print(f"| {region.upper()} "
                  f"| {r.get('count', 0)} "
                  f"| {format_val(r.get('mean'))} "
                  f"| {format_val(r.get('p95'))} "
                  f"| {format_val(r.get('p99'))} "
                  f"| {format_val(r.get('pct_under_5s'), '%')} |")
        print()

    # --- Comparison with previous run ---
    if previous is not None:
        prev_overall = previous.get("overall", {})
        prev_ts = previous.get("timestamp", "unknown")
        print("## Performance Over Time")
        print()
        print(f"Compared with previous run ({prev_ts}):")
        print()
        print("| Metric | Previous | Current | Delta |")
        print("|--------|----------|---------|-------|")
        for metric in ["mean", "p95", "p99"]:
            prev_v = prev_overall.get(metric)
            curr_v = overall.get(metric)
            if prev_v is not None and curr_v is not None:
                delta = curr_v - prev_v
                sign = "+" if delta > 0 else ""
                indicator = "🔺" if delta > 0.5 else ("🔽" if delta < -0.5 else "➖")
                print(f"| {metric.upper()} | {prev_v}s | {curr_v}s | {sign}{delta:.3f}s {indicator} |")
            else:
                print(f"| {metric.upper()} | {format_val(prev_v)} | {format_val(curr_v)} | - |")

        prev_pct = prev_overall.get("pct_under_5s")
        curr_pct = overall.get("pct_under_5s")
        if prev_pct is not None and curr_pct is not None:
            delta = curr_pct - prev_pct
            sign = "+" if delta > 0 else ""
            print(f"| Under 5s | {prev_pct}% | {curr_pct}% | {sign}{delta:.1f}% |")
        print()

    # --- Baseline comparison (PR only) ---
    if baseline is not None:
        print("## Main Branch Baseline Comparison")
        print()
        print(f"Baseline computed from {baseline['run_count']} recent main branch run(s):")
        print()
        print("| Metric | Baseline Avg | This PR | Delta |")
        print("|--------|-------------|---------|-------|")
        comparisons = [
            ("Mean", "avg_mean", "mean"),
            ("P95", "avg_p95", "p95"),
            ("P99", "avg_p99", "p99"),
        ]
        for label, base_key, curr_key in comparisons:
            base_v = baseline.get(base_key)
            curr_v = overall.get(curr_key)
            if base_v is not None and curr_v is not None:
                delta = curr_v - base_v
                sign = "+" if delta > 0 else ""
                indicator = "🔺" if delta > 0.5 else ("🔽" if delta < -0.5 else "➖")
                print(f"| {label} | {base_v}s | {curr_v}s | {sign}{delta:.3f}s {indicator} |")
            else:
                print(f"| {label} | {format_val(base_v)} | {format_val(curr_v)} | - |")

        base_pct = baseline.get("avg_pct_under_5s")
        curr_pct = overall.get("pct_under_5s")
        if base_pct is not None and curr_pct is not None:
            delta = curr_pct - base_pct
            sign = "+" if delta > 0 else ""
            print(f"| Under 5s | {base_pct}% | {curr_pct}% | {sign}{delta:.1f}% |")
        print()

        # Show individual baseline runs
        if baseline.get("individual_runs"):
            print("<details>")
            print("<summary>Individual baseline runs</summary>")
            print()
            print("| Run | Mean | P95 | Under 5s |")
            print("|-----|------|-----|----------|")
            for i, run in enumerate(baseline["individual_runs"], 1):
                print(f"| {run.get('timestamp', f'Run {i}')} "
                      f"| {format_val(run.get('mean'))} "
                      f"| {format_val(run.get('p95'))} "
                      f"| {format_val(run.get('pct_under_5s'), '%')} |")
            print()
            print("</details>")
            print()

    # --- Dashboard screenshots ---
    screenshots = [
        ("global-alert-overview", "Global Alert Overview"),
        ("regional-service-health-apac", "Regional Service Health (APAC)"),
        ("regional-service-health-eu", "Regional Service Health (EU)"),
        ("regional-service-health-us", "Regional Service Health (US)"),
    ]

    has_screenshots = False
    for name, _ in screenshots:
        if os.path.exists(os.path.join(OUTPUT_DIR, f"{name}.png")):
            has_screenshots = True
            break

    if has_screenshots:
        print("## Dashboard Screenshots")
        print()
        for name, title in screenshots:
            path = os.path.join(OUTPUT_DIR, f"{name}.png")
            data_uri = image_to_data_uri(path)
            if data_uri:
                print(f"### {title}")
                print()
                print(f'<img src="{data_uri}" alt="{title}" width="100%">')
                print()


if __name__ == "__main__":
    main()
