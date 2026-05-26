"""Capture comprehensive logs + outputs for one sample's GSE runs.

Writes a self-contained directory under sample-logs/<sample>/<cohort>/
containing get_run, list_run_tasks, run/engine/manifest log streams,
and (for failed tasks) per-task log streams.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError


def jdump(p: Path, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=2, default=str))


def safe(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except Exception as e:  # noqa: BLE001
        return {"_error": f"{type(e).__name__}: {e}"}


def collect_run(omics, logs, *, run_id: str, module: str, dst: Path):
    info = safe(omics.get_run, id=run_id)
    rd = dst / f"{module}-{run_id}"
    rd.mkdir(parents=True, exist_ok=True)
    jdump(rd / "get-run.json", info)

    if "_error" in info:
        return

    # Tasks
    tasks_items = []
    next_token = None
    while True:
        kw = {"id": run_id, "maxResults": 100}
        if next_token:
            kw["startingToken"] = next_token
        resp = safe(omics.list_run_tasks, **kw)
        if not isinstance(resp, dict) or "_error" in resp:
            tasks_items = resp
            break
        tasks_items.extend(resp.get("items", []))
        next_token = resp.get("nextToken")
        if not next_token:
            break
    jdump(rd / "list-run-tasks.json", {"items": tasks_items} if isinstance(tasks_items, list) else tasks_items)

    # Per-task durations (handy summary)
    durations = []
    if isinstance(tasks_items, list):
        for t in tasks_items:
            start, stop = t.get("startTime"), t.get("stopTime")
            dur = (stop - start).total_seconds() if start and stop else None
            durations.append({
                "taskId": t.get("taskId"),
                "name": t.get("name"),
                "status": t.get("status"),
                "instanceType": t.get("instanceType"),
                "cpus": t.get("cpus"),
                "memory": t.get("memory"),
                "duration_seconds": dur,
            })
    jdump(rd / "task-durations.json", durations)

    # Run-level log streams. The orchestrator-engine + manifest streams have
    # well-known names per HealthOmics docs.
    uuid = info.get("uuid", "")
    for kind, stream in [
        ("run", f"run/{run_id}"),
        ("engine", f"run/{run_id}/engine"),
        ("manifest", f"manifest/run/{run_id}/{uuid}"),
    ]:
        evs = safe(
            logs.get_log_events,
            logGroupName="/aws/omics/WorkflowLog",
            logStreamName=stream,
            startFromHead=True,
            limit=10000,
        )
        jdump(rd / f"log-{kind}.json", evs)

    # Per-task logs (just for the failed ones; for COMPLETED runs we still
    # capture the longest task to show typical content).
    task_log_dir = rd / "task-logs"
    task_log_dir.mkdir(exist_ok=True)
    if isinstance(durations, list):
        targets = [d for d in durations if d["status"] != "COMPLETED"]
        if not targets and durations:
            # Capture the longest-running task as a representative
            targets = [max(durations, key=lambda d: (d.get("duration_seconds") or 0))]
        for d in targets:
            tid = d.get("taskId")
            if not tid:
                continue
            evs = safe(
                logs.get_log_events,
                logGroupName="/aws/omics/WorkflowLog",
                logStreamName=f"run/{run_id}/task/{tid}",
                startFromHead=True,
                limit=2000,
            )
            jdump(task_log_dir / f"task-{tid}.json", evs)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", required=True)
    ap.add_argument("--manifests", nargs="+",
                    default=[
                        "gse-cohort-runs-gatk-sv-validation-2026q2-rerun-2026-05-25.json",
                        "gse-cohort-runs-gatk-sv-validation-2026q2-rerun-2026-05-25-scramble.json",
                        "gse-cohort-runs-customer-sim.json",
                    ])
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    runs = []
    for path in args.manifests:
        if not Path(path).exists():
            continue
        for r in json.load(open(path))["runs"]:
            if r.get("sample") == args.sample:
                runs.append(r)

    if not runs:
        print(f"No runs found for sample {args.sample}.", file=sys.stderr)
        return 1

    out_dir = Path(args.out or f"sample-logs/{args.sample}")
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Capturing logs for {len(runs)} {args.sample} runs to {out_dir}/")

    omics = boto3.client("omics", region_name="ap-southeast-1")
    logs = boto3.client("logs", region_name="ap-southeast-1")
    for r in runs:
        print(f"  {r['module']:<10s} run={r['id']}")
        collect_run(omics, logs, run_id=r["id"], module=r["module"], dst=out_dir)

    print()
    print(f"Done. Inspect with: ls -la {out_dir}/*/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
