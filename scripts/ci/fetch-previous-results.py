#!/usr/bin/env python3
"""Fetch results from the previous successful performance test run.

Uses the GitHub CLI to find the most recent successful run of this
workflow on the same branch and downloads its results artifact.
"""

import json
import os
import subprocess
import sys
import zipfile

OUTPUT_DIR = ".perf-results"
WORKFLOW_NAME = "performance.yml"


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

    # Find the most recent successful run of this workflow on main
    branch = os.environ.get("GITHUB_REF_NAME", "main")
    runs_json = run_gh(
        "run", "list",
        "--workflow", WORKFLOW_NAME,
        "--branch", branch,
        "--status", "success",
        "--limit", "1",
        "--json", "databaseId,conclusion,headBranch",
    )

    if not runs_json:
        print("No previous successful runs found.")
        return

    runs = json.loads(runs_json)
    if not runs:
        print("No previous successful runs found.")
        return

    run_id = str(runs[0]["databaseId"])
    print(f"Found previous run: {run_id}")

    # Download the artifact
    prev_dir = os.path.join(OUTPUT_DIR, "previous")
    os.makedirs(prev_dir, exist_ok=True)

    result = subprocess.run(
        ["gh", "run", "download", run_id,
         "--name", "performance-results",
         "--dir", prev_dir],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print(f"Could not download artifact: {result.stderr}")
        return

    prev_results = os.path.join(prev_dir, "results.json")
    if os.path.exists(prev_results):
        print(f"Previous results downloaded to {prev_results}")
        with open(prev_results) as f:
            data = json.load(f)
        print(f"  Previous run timestamp: {data.get('timestamp', 'unknown')}")
        overall = data.get("overall", {})
        if overall.get("mean"):
            print(f"  Previous mean: {overall['mean']:.3f}s")
    else:
        print("Artifact downloaded but results.json not found.")


if __name__ == "__main__":
    main()
