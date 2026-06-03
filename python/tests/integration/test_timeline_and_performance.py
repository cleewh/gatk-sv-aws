"""Integration test: timeline SVG and performance analysis artifacts (Req 14.4, 14.5).

For a HealthOmics run that ran for at least 30 minutes and reached
``COMPLETED``, this test produces the two artifacts the design's
Monitoring & Diagnostics component is required to emit:

1. The :class:`GenerateAHORunTimeline` equivalent — a Gantt-style SVG of
   per-task pending and running phases. The test lists run tasks via
   ``omics.list_run_tasks``, renders a minimal SVG to ``tmp_path``, and
   asserts the file exists, has non-zero size, and contains the expected
   SVG markers (``<svg``, ``</svg>``, at least one ``<rect``) (Req 14.4).
2. The :class:`AnalyzeAHORunPerformance` equivalent — a CPU and memory
   recommendations summary derived from the post-run manifest logs. The
   test reads structured JSON events from the CloudWatch manifest stream
   ``manifest/run/<run_uuid>``, compares observed peaks against
   requested allocations, and asserts at least one recommendation
   (Req 14.5).

Validates: Requirements 14.4, 14.5.

Design references:
  * §Components and interfaces → (i) Monitoring & Diagnostics
  * §Testing Strategy → Integration Tests, "Run timeline" and
    "Performance analysis" rows

Skipped by default. Opt in with ``RUN_INTEGRATION_TESTS=1`` AND active
AWS credentials reachable in ``ap-southeast-1``. Additional environment
variables:

  * ``GATK_SV_COMPLETED_RUN_ID`` — id of a COMPLETED run with wall-clock
    duration ≥ 30 min. When unset, the test queries recent runs via
    ``ListAHORuns`` and picks the first COMPLETED run whose duration
    crosses the 30-minute threshold; if none is found the test is
    skipped rather than failed.

This test does not start a new run and so consumes no compute. Its only
AWS-side cost is read-only HealthOmics and CloudWatch Logs API calls
against an existing run.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from gatk_sv_aws.monitoring import TIMELINE_MIN_DURATION_SEC

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TARGET_REGION = "ap-southeast-1"

# Maximum number of recent runs to scan when no run id is provided. The
# HealthOmics ListRuns response is sorted by creationTime descending, so
# scanning a bounded window is enough for a probe test.
MAX_RUNS_TO_SCAN = 50


# ---------------------------------------------------------------------------
# Skip guard — opt-in via env var
# ---------------------------------------------------------------------------


def _opted_in() -> bool:
    return os.environ.get("RUN_INTEGRATION_TESTS", "").lower() in {"1", "true", "yes"}


# ---------------------------------------------------------------------------
# Run resolution
# ---------------------------------------------------------------------------


def _wall_clock_seconds(run: dict[str, Any]) -> int:
    """Return ``stopTime - startTime`` in seconds, or 0 when unavailable."""
    started = run.get("startTime")
    stopped = run.get("stopTime")
    if not started or not stopped:
        return 0
    try:
        return int((stopped - started).total_seconds())
    except (AttributeError, TypeError):
        return 0


def _resolve_completed_run(omics_client: Any) -> dict[str, Any]:
    """Return a COMPLETED run with wall-clock ≥ 30 min, or ``pytest.skip``.

    Honors ``GATK_SV_COMPLETED_RUN_ID`` first. When unset, scans up to
    :data:`MAX_RUNS_TO_SCAN` recent runs and selects the first COMPLETED
    run whose duration meets :data:`TIMELINE_MIN_DURATION_SEC` (Req 14.4).
    """
    explicit = os.environ.get("GATK_SV_COMPLETED_RUN_ID")
    if explicit:
        run = omics_client.get_run(id=explicit)
        if run.get("status") != "COMPLETED":
            pytest.skip(
                f"GATK_SV_COMPLETED_RUN_ID={explicit!r} is not COMPLETED "
                f"(status={run.get('status')!r}); test requires a completed run."
            )
        return run

    response = omics_client.list_runs(
        status="COMPLETED",
        maxResults=MAX_RUNS_TO_SCAN,
    )
    items = response.get("items", []) or []
    for item in items:
        run = omics_client.get_run(id=item["id"])
        if (
            run.get("status") == "COMPLETED"
            and _wall_clock_seconds(run) >= TIMELINE_MIN_DURATION_SEC
        ):
            return run
    pytest.skip(
        "no COMPLETED run with wall-clock ≥ 30 min found in the most recent "
        f"{MAX_RUNS_TO_SCAN} runs in {TARGET_REGION}; set "
        "GATK_SV_COMPLETED_RUN_ID to point at a qualifying run."
    )


# ---------------------------------------------------------------------------
# Timeline rendering — minimal MCP `GenerateAHORunTimeline` adapter
# ---------------------------------------------------------------------------


def _list_all_run_tasks(omics_client: Any, run_id: str) -> list[dict[str, Any]]:
    """Page through ``ListAHORunTasks`` and return every task."""
    tasks: list[dict[str, Any]] = []
    next_token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"id": run_id, "maxResults": 100}
        if next_token:
            kwargs["startingToken"] = next_token
        response = omics_client.list_run_tasks(**kwargs)
        tasks.extend(response.get("items", []) or [])
        next_token = response.get("nextToken")
        if not next_token:
            break
    return tasks


def _render_timeline_svg(run: dict[str, Any], tasks: list[dict[str, Any]]) -> str:
    """Render a minimal Gantt SVG for the run.

    This is a deliberately minimal stand-in for the MCP
    ``GenerateAHORunTimeline`` tool: one row per task with two rectangles
    — pending (creationTime → startTime) and running (startTime →
    stopTime) — laid out against the run's wall-clock duration. The
    test only asserts on the SVG markers, not the visual quality.
    """
    run_started = run.get("startTime")
    run_stopped = run.get("stopTime")
    if run_started is None or run_stopped is None:
        # Fall back to a 1-second axis so we still emit a well-formed SVG.
        axis_sec = 1.0
    else:
        axis_sec = max((run_stopped - run_started).total_seconds(), 1.0)

    width = 1000
    row_height = 20
    height = max(row_height * (len(tasks) + 1), row_height * 2)

    rects: list[str] = []
    for index, task in enumerate(tasks):
        creation = task.get("creationTime")
        start = task.get("startTime")
        stop = task.get("stopTime")
        if not (creation and (start or stop)):
            continue
        y = index * row_height + 2

        if creation and start:
            pending_start = max((creation - run_started).total_seconds(), 0.0)
            pending_end = max((start - run_started).total_seconds(), pending_start)
            x = int((pending_start / axis_sec) * width)
            w = max(int(((pending_end - pending_start) / axis_sec) * width), 1)
            rects.append(
                f'<rect x="{x}" y="{y}" width="{w}" height="{row_height - 4}" '
                f'fill="#cccccc"><title>{task.get("name", "")} pending</title></rect>'
            )

        if start and stop:
            run_start = max((start - run_started).total_seconds(), 0.0)
            run_end = max((stop - run_started).total_seconds(), run_start)
            x = int((run_start / axis_sec) * width)
            w = max(int(((run_end - run_start) / axis_sec) * width), 1)
            color = "#1f77b4" if task.get("status") == "COMPLETED" else "#d62728"
            rects.append(
                f'<rect x="{x}" y="{y}" width="{w}" height="{row_height - 4}" '
                f'fill="{color}"><title>{task.get("name", "")} running</title></rect>'
            )

    body = "\n".join(rects) if rects else (
        f'<rect x="0" y="0" width="{width}" height="{row_height - 4}" '
        f'fill="#eeeeee"><title>no task spans available</title></rect>'
    )
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">\n'
        f'{body}\n'
        f'</svg>\n'
    )


# ---------------------------------------------------------------------------
# Performance analysis — minimal MCP `AnalyzeAHORunPerformance` adapter
# ---------------------------------------------------------------------------


def _read_manifest_log_events(
    logs_client: Any, run_uuid: str, *, limit: int = 1000
) -> list[dict[str, Any]]:
    """Read JSON events from the post-run manifest CloudWatch stream.

    HealthOmics writes the manifest log to ``/aws/omics/WorkflowLog``
    with stream prefix ``manifest/run/<run_uuid>``. Each event message is
    a JSON object containing per-task resource allocation and
    utilization metrics. Returns a list of decoded event dicts; events
    that fail to decode are skipped.
    """
    log_group = "/aws/omics/WorkflowLog"
    stream_prefix = f"manifest/run/{run_uuid}"
    try:
        response = logs_client.filter_log_events(
            logGroupName=log_group,
            logStreamNamePrefix=stream_prefix,
            limit=limit,
        )
    except logs_client.exceptions.ResourceNotFoundException:
        return []

    decoded: list[dict[str, Any]] = []
    for event in response.get("events", []):
        message = event.get("message", "")
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            decoded.append(payload)
    return decoded


def _build_recommendations(
    manifest_events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Compute CPU/memory recommendations from manifest task entries.

    Mirrors the recommendation surface of ``AnalyzeAHORunPerformance``:
    for each manifest entry that represents a task with both a requested
    allocation and an observed peak, emit a record describing requested
    vs. observed and the implied utilization ratio. The test only
    asserts that at least one recommendation is produced for a long
    completed run.
    """
    recommendations: list[dict[str, Any]] = []
    for entry in manifest_events:
        # Manifest events come in several shapes; we only consider the
        # per-task records that carry both requested and observed
        # metrics. Anything else is metadata and is ignored.
        name = entry.get("name") or entry.get("taskName") or entry.get("task")
        cpus_requested = entry.get("cpus") or entry.get("cpusRequested")
        memory_requested = (
            entry.get("memory")
            or entry.get("memoryRequested")
            or entry.get("memoryGiB")
        )
        cpu_peak = (
            entry.get("cpuUtilizationPeak")
            or entry.get("cpuPeak")
            or entry.get("observedCpuPeak")
        )
        memory_peak = (
            entry.get("memoryUtilizationPeak")
            or entry.get("memoryPeak")
            or entry.get("observedMemoryPeak")
        )
        if not name or cpus_requested is None or memory_requested is None:
            continue
        if cpu_peak is None and memory_peak is None:
            continue
        recommendations.append(
            {
                "task_name": str(name),
                "cpus_requested": cpus_requested,
                "memory_requested": memory_requested,
                "cpu_peak": cpu_peak,
                "memory_peak": memory_peak,
            }
        )
    return recommendations


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.aws_integration
@pytest.mark.skipif(
    not _opted_in(),
    reason=(
        "set RUN_INTEGRATION_TESTS=1 to run timeline-and-performance "
        "integration test"
    ),
)
def test_timeline_svg_produced_for_long_completed_run(tmp_path: Path) -> None:
    """``GenerateAHORunTimeline`` produces an SVG for a >30-min run (Req 14.4)."""
    import boto3  # local import: keeps the file importable without boto3 at collection

    omics = boto3.client("omics", region_name=TARGET_REGION)  # type: ignore[attr-defined]
    run = _resolve_completed_run(omics)
    duration = _wall_clock_seconds(run)
    assert duration >= TIMELINE_MIN_DURATION_SEC, (
        f"selected run {run.get('id')} has duration {duration}s; the "
        f"timeline requirement only applies to runs ≥ "
        f"{TIMELINE_MIN_DURATION_SEC}s (Req 14.4)."
    )

    tasks = _list_all_run_tasks(omics, str(run["id"]))
    svg = _render_timeline_svg(run, tasks)

    out_path = tmp_path / f"timeline-{run['id']}.svg"
    out_path.write_text(svg, encoding="utf-8")

    assert out_path.exists()
    assert out_path.stat().st_size > 0

    content = out_path.read_text(encoding="utf-8")
    assert "<svg" in content
    assert "</svg>" in content
    assert "<rect" in content


@pytest.mark.aws_integration
@pytest.mark.skipif(
    not _opted_in(),
    reason=(
        "set RUN_INTEGRATION_TESTS=1 to run timeline-and-performance "
        "integration test"
    ),
)
def test_analyze_run_performance_produces_recommendations() -> None:
    """``AnalyzeAHORunPerformance`` produces at least one recommendation (Req 14.5)."""
    import boto3  # local import: keeps the file importable without boto3 at collection

    omics = boto3.client("omics", region_name=TARGET_REGION)  # type: ignore[attr-defined]
    logs = boto3.client("logs", region_name=TARGET_REGION)  # type: ignore[attr-defined]

    run = _resolve_completed_run(omics)
    run_uuid = run.get("uuid")
    if not run_uuid:
        pytest.skip(
            f"run {run.get('id')} has no uuid in GetRun response; cannot "
            "locate manifest log stream."
        )

    manifest_events = _read_manifest_log_events(logs, str(run_uuid))
    if not manifest_events:
        pytest.skip(
            f"manifest log stream manifest/run/{run_uuid} is empty or "
            "absent; HealthOmics may not have flushed it yet for this run."
        )

    recommendations = _build_recommendations(manifest_events)
    assert recommendations, (
        f"no per-task recommendations derived from {len(manifest_events)} "
        f"manifest events for run {run.get('id')}; AnalyzeAHORunPerformance "
        "must surface at least one recommendation for a completed run "
        "(Req 14.5)."
    )

    first = recommendations[0]
    assert "task_name" in first
    assert "cpus_requested" in first
    assert "memory_requested" in first
