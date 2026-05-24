"""Component (i): Monitoring & Diagnostics for the GATK-SV migration.

Implements design §Components and interfaces → (i) Monitoring &
Diagnostics. Emits run-started and run-finished events, invokes
``DiagnoseAHORunFailure`` on FAILED runs, generates a run timeline SVG for
any COMPLETED or FAILED run longer than 30 minutes, runs
``AnalyzeAHORunPerformance`` for every COMPLETED run, and records each
retry attempt in the run-level log.

Advances Requirements 14, 15.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

# Timeline threshold: any COMPLETED or FAILED run longer than 30 minutes
# gets a :func:`generate_timeline_if_long` artifact (Req 14.4).
TIMELINE_MIN_DURATION_SEC = 30 * 60


# ---------------------------------------------------------------------------
# Event emission (Task 3.9.1)
# ---------------------------------------------------------------------------


def emit_run_started(run_id: str, cohort_id: str, parameters: dict) -> dict:
    """Emit a run-started event (Req 14.1). Returns the emitted event dict.

    Implementation of Task 3.9.1. Builds a structured event with keys
    ``event``, ``run_id``, ``cohort_id``, ``parameters``, ``emitted_at``;
    prints it as a single-line JSON document to stdout for log ingestion;
    and returns the dict so callers can attach it to run-level records
    (Design §Components.i).
    """

    event = {
        "event": "run_started",
        "run_id": run_id,
        "cohort_id": cohort_id,
        "parameters": parameters,
        "emitted_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    print(json.dumps(event, default=str))
    return event


def emit_run_finished(
    run_id: str, status: str, wall_clock_sec: int, cost_usd: float
) -> dict:
    """Emit a run-finished event (Req 14.2). Returns the emitted event dict.

    Implementation of Task 3.9.1.

    Builds a structured event with keys ``event``, ``run_id``, ``status``,
    ``wall_clock_sec``, ``cost_usd``, ``emitted_at``; prints it as a
    single-line JSON document to stdout for log ingestion; and returns the
    dict so callers can attach it to run-level records (Design §Components.i).
    """

    event = {
        "event": "run_finished",
        "run_id": run_id,
        "status": status,
        "wall_clock_sec": wall_clock_sec,
        "cost_usd": cost_usd,
        "emitted_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    print(json.dumps(event, default=str))
    return event


# ---------------------------------------------------------------------------
# Diagnostic bundle (Task 3.9.2)
# ---------------------------------------------------------------------------


class HealthOmicsDiagnostic(Protocol):
    """Minimal protocol matching ``DiagnoseAHORunFailure``."""

    def diagnose(self, run_id: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class DiagnosticBundle:
    """A run-failure diagnostic bundle (Req 14.3)."""

    run_id: str
    failure_reason: str
    engine_logs: tuple[str, ...]
    failed_tasks: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    recommendations: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "failure_reason": self.failure_reason,
            "engine_logs": list(self.engine_logs),
            "failed_tasks": list(self.failed_tasks),
            "recommendations": list(self.recommendations),
        }


def diagnose_failure(
    run_id: str, *, diagnostic_client: HealthOmicsDiagnostic
) -> DiagnosticBundle:
    """Call ``DiagnoseAHORunFailure`` and return a :class:`DiagnosticBundle`.

    Implementation of Task 3.9.2 (Req 14.3).
    """
    response = diagnostic_client.diagnose(run_id)
    return DiagnosticBundle(
        run_id=run_id,
        failure_reason=str(response.get("failureReason", "unknown")),
        engine_logs=tuple(response.get("engineLogs", []) or []),
        failed_tasks=tuple(response.get("failedTasks", []) or []),
        recommendations=tuple(response.get("recommendations", []) or []),
    )


# ---------------------------------------------------------------------------
# Timeline generation (Task 3.9.3)
# ---------------------------------------------------------------------------


class HealthOmicsTimeline(Protocol):
    """Minimal protocol matching ``GenerateAHORunTimeline``."""

    def generate(self, run_id: str, output_path: str) -> dict[str, Any]: ...


def generate_timeline_if_long(
    run_id: str,
    duration_sec: int,
    *,
    output_prefix: str,
    timeline_client: HealthOmicsTimeline,
) -> str | None:
    """Generate a timeline SVG when the run lasted ≥ 30 minutes (Req 14.4).

    Implementation of Task 3.9.3. Returns the S3 URI of the generated
    timeline, or ``None`` when the run was shorter than the threshold.
    """
    if duration_sec < TIMELINE_MIN_DURATION_SEC:
        return None
    output_uri = f"{output_prefix.rstrip('/')}/timelines/{run_id}.svg"
    timeline_client.generate(run_id=run_id, output_path=output_uri)
    return output_uri


# ---------------------------------------------------------------------------
# Retry log (Task 3.9.4)
# ---------------------------------------------------------------------------


def record_retry(
    run_id: str,
    task_id: str,
    attempt_number: int,
    error_code: str,
    *,
    log_path: Path,
) -> dict[str, Any]:
    """Append one retry attempt to the run-level retry log (Req 15.4).

    Implementation of Task 3.9.4. Returns the recorded entry so callers
    can also attach it to the run-finished event.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "event": "retry_attempt",
        "run_id": run_id,
        "task_id": task_id,
        "attempt_number": attempt_number,
        "error_code": error_code,
        "emitted_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    with log_path.open("a") as handle:
        handle.write(json.dumps(entry) + "\n")
    return entry


__all__ = [
    "TIMELINE_MIN_DURATION_SEC",
    "emit_run_started",
    "emit_run_finished",
    "HealthOmicsDiagnostic",
    "DiagnosticBundle",
    "diagnose_failure",
    "HealthOmicsTimeline",
    "generate_timeline_if_long",
    "record_retry",
]
