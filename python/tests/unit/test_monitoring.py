"""Unit tests for the GATK-SV Monitoring & Diagnostics component (Design §Components.i).

These example-based tests complement the Hypothesis property tests in
``tests/gatk_sv_aws/properties/`` by pinning:

* :func:`emit_run_started` returned-dict shape and stdout JSON line (Req 14.1).
* :func:`emit_run_finished` returned-dict shape and stdout JSON line (Req 14.2).
* ``emitted_at`` is an ISO-8601 UTC timestamp.
* The printed line round-trips through :mod:`json` as a single object
  identical (modulo serialization) to the returned dict.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from gatk_sv_aws.monitoring import (
    emit_run_finished,
    emit_run_started,
)


def _assert_iso_utc(value: str) -> None:
    """The ``emitted_at`` field must parse as a timezone-aware ISO-8601 datetime."""
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None


# ---------------------------------------------------------------------------
# emit_run_started (Req 14.1)
# ---------------------------------------------------------------------------


def test_emit_run_started_returns_expected_keys() -> None:
    params = {"reference_fasta": "s3://bucket/GRCh38.fasta", "batch_name": "b1"}

    event = emit_run_started(
        run_id="run-abc123",
        cohort_id="cohort-sg-2025q1",
        parameters=params,
    )

    assert event["event"] == "run_started"
    assert event["run_id"] == "run-abc123"
    assert event["cohort_id"] == "cohort-sg-2025q1"
    assert event["parameters"] == params
    assert "emitted_at" in event
    _assert_iso_utc(event["emitted_at"])


def test_emit_run_started_prints_single_line_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    event = emit_run_started(
        run_id="run-1",
        cohort_id="cohort-1",
        parameters={"k": "v"},
    )

    captured = capsys.readouterr().out
    # Exactly one line on stdout (plus trailing newline from print).
    lines = [line for line in captured.splitlines() if line]
    assert len(lines) == 1

    decoded = json.loads(lines[0])
    assert decoded == event


# ---------------------------------------------------------------------------
# emit_run_finished (Req 14.2)
# ---------------------------------------------------------------------------


def test_emit_run_finished_returns_expected_keys() -> None:
    event = emit_run_finished(
        run_id="run-xyz789",
        status="COMPLETED",
        wall_clock_sec=7200,
        cost_usd=123.45,
    )

    assert event["event"] == "run_finished"
    assert event["run_id"] == "run-xyz789"
    assert event["status"] == "COMPLETED"
    assert event["wall_clock_sec"] == 7200
    assert event["cost_usd"] == 123.45
    assert "emitted_at" in event
    _assert_iso_utc(event["emitted_at"])


def test_emit_run_finished_prints_single_line_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    event = emit_run_finished(
        run_id="run-2",
        status="FAILED",
        wall_clock_sec=0,
        cost_usd=0.0,
    )

    captured = capsys.readouterr().out
    lines = [line for line in captured.splitlines() if line]
    assert len(lines) == 1

    decoded = json.loads(lines[0])
    assert decoded == event


# ---------------------------------------------------------------------------
# Cross-event sanity: both events emit exactly one parseable JSON object
# ---------------------------------------------------------------------------


def test_both_events_produce_parseable_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    started = emit_run_started("run-3", "cohort-3", {"x": 1})
    finished = emit_run_finished("run-3", "COMPLETED", 60, 0.5)

    captured = capsys.readouterr().out
    lines = [line for line in captured.splitlines() if line]
    assert len(lines) == 2

    assert json.loads(lines[0]) == started
    assert json.loads(lines[1]) == finished


# ---------------------------------------------------------------------------
# diagnose_failure (Req 14.3)
# ---------------------------------------------------------------------------


class FakeDiagnosticClient:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.calls: list[str] = []

    def diagnose(self, run_id: str) -> dict:
        self.calls.append(run_id)
        return self.response


def test_diagnose_failure_wraps_mcp_response() -> None:
    from gatk_sv_aws.monitoring import diagnose_failure

    client = FakeDiagnosticClient(
        {
            "failureReason": "Task failed with exit code 1",
            "engineLogs": ["ERROR: cromwell task failed"],
            "failedTasks": [{"taskId": "t1", "name": "Manta"}],
            "recommendations": ["Check container image availability"],
        }
    )

    bundle = diagnose_failure("run-123", diagnostic_client=client)

    assert client.calls == ["run-123"]
    assert bundle.run_id == "run-123"
    assert bundle.failure_reason == "Task failed with exit code 1"
    assert len(bundle.engine_logs) == 1
    assert len(bundle.failed_tasks) == 1
    assert bundle.recommendations == ("Check container image availability",)
    # to_dict preserves every field
    assert bundle.to_dict()["failure_reason"] == "Task failed with exit code 1"


# ---------------------------------------------------------------------------
# generate_timeline_if_long (Req 14.4)
# ---------------------------------------------------------------------------


class FakeTimelineClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def generate(self, run_id: str, output_path: str) -> dict:
        self.calls.append({"run_id": run_id, "output_path": output_path})
        return {"status": "ok"}


def test_timeline_skipped_under_threshold() -> None:
    from gatk_sv_aws.monitoring import generate_timeline_if_long

    client = FakeTimelineClient()
    result = generate_timeline_if_long(
        "run-short",
        duration_sec=15 * 60,  # 15 min
        output_prefix="s3://bkt/cohort-1",
        timeline_client=client,
    )
    assert result is None
    assert client.calls == []


def test_timeline_generated_at_or_above_threshold() -> None:
    from gatk_sv_aws.monitoring import generate_timeline_if_long

    client = FakeTimelineClient()
    result = generate_timeline_if_long(
        "run-long",
        duration_sec=31 * 60,  # 31 min
        output_prefix="s3://bkt/cohort-1/",
        timeline_client=client,
    )
    assert result == "s3://bkt/cohort-1/timelines/run-long.svg"
    assert len(client.calls) == 1
    assert client.calls[0]["run_id"] == "run-long"


# ---------------------------------------------------------------------------
# record_retry (Req 15.4)
# ---------------------------------------------------------------------------


def test_record_retry_appends_jsonl(tmp_path) -> None:
    from gatk_sv_aws.monitoring import record_retry

    log_path = tmp_path / "retries" / "run-123.jsonl"
    entry1 = record_retry(
        "run-123",
        "task-abc",
        attempt_number=1,
        error_code="Throttling",
        log_path=log_path,
    )
    entry2 = record_retry(
        "run-123",
        "task-abc",
        attempt_number=2,
        error_code="Throttling",
        log_path=log_path,
    )

    assert entry1["event"] == "retry_attempt"
    assert entry1["attempt_number"] == 1
    assert entry2["attempt_number"] == 2

    lines = [line for line in log_path.read_text().splitlines() if line]
    assert len(lines) == 2
    assert json.loads(lines[0])["attempt_number"] == 1
    assert json.loads(lines[1])["error_code"] == "Throttling"
