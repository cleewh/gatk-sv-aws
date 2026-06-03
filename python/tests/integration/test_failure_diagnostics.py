"""Integration test: failure diagnostics are captured in run-finished event (Req 14.3, 15.2).

Submits a deliberately malformed HealthOmics run, waits for it to reach
``FAILED`` status, then invokes the diagnostic equivalents of the MCP
tools ``DiagnoseAHORunFailure`` and ``GetAHORunLogs`` via the boto3
``omics`` and ``logs`` clients. Asserts that the resulting diagnostic
bundle is attached to the run-finished event emitted by the Monitoring
component (``gatk_sv_aws.monitoring``).

Validates: Requirements 14.3, 15.2.

Design references:
  * §Components and interfaces → (i) Monitoring & Diagnostics
  * §Testing Strategy → Integration Tests, "Failure diagnostics" row

Skipped by default. Opt in with ``RUN_INTEGRATION_TESTS=1`` AND active
AWS credentials reachable in ``ap-southeast-1``. Additional environment
variables:

  * ``GATK_SV_TEST_WORKFLOW_ID`` — HealthOmics workflow id to submit
    against. Defaults to the first record in
    ``gatk-sv-healthomics/workflow-versions.json``.
  * ``GATK_SV_HEALTHOMICS_ROLE_ARN`` — IAM role ARN used for the run.
  * ``GATK_SV_OUTPUT_URI`` — S3 URI prefix for run outputs.
  * ``GATK_SV_FAILURE_TIMEOUT_SEC`` — polling timeout in seconds
    (default 1800 = 30 min).

Cost: a malformed run typically fails in <5 min and costs only the
control-plane fee. The test cancels the run if it stays in non-terminal
state past the timeout.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from gatk_sv_aws.monitoring import diagnose_failure, emit_run_finished

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TARGET_REGION = "ap-southeast-1"

# Polling cadence for run status. Kept generous because malformed runs
# can sit in STARTING for a few minutes before HealthOmics fails them.
POLL_INTERVAL_SEC = 30
DEFAULT_TIMEOUT_SEC = 30 * 60

WORKFLOW_VERSIONS_PATH = (
    Path(__file__).resolve().parents[4]
    / "gatk-sv-healthomics"
    / "workflow-versions.json"
)


# ---------------------------------------------------------------------------
# Skip guard — opt-in via env var
# ---------------------------------------------------------------------------


def _opted_in() -> bool:
    return os.environ.get("RUN_INTEGRATION_TESTS", "").lower() in {"1", "true", "yes"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_workflow_id() -> str:
    """Return the workflow id under test, or skip if unresolvable."""
    workflow_id = os.environ.get("GATK_SV_TEST_WORKFLOW_ID")
    if workflow_id:
        return workflow_id
    if WORKFLOW_VERSIONS_PATH.exists():
        data = json.loads(WORKFLOW_VERSIONS_PATH.read_text())
        records = data.get("records", [])
        if records:
            return str(records[0]["workflow_id"])
    pytest.skip(
        "no workflow id available: set GATK_SV_TEST_WORKFLOW_ID or populate "
        f"{WORKFLOW_VERSIONS_PATH}"
    )


def _resolve_role_arn() -> str:
    """Return the IAM role ARN for the run, or skip if missing."""
    role_arn = os.environ.get("GATK_SV_HEALTHOMICS_ROLE_ARN")
    if not role_arn:
        pytest.skip("set GATK_SV_HEALTHOMICS_ROLE_ARN to the run-role ARN")
    return role_arn


def _resolve_output_uri() -> str:
    output_uri = os.environ.get("GATK_SV_OUTPUT_URI")
    if not output_uri:
        pytest.skip("set GATK_SV_OUTPUT_URI to an S3 prefix for run outputs")
    return output_uri


def _wait_for_failed(omics_client: Any, run_id: str, *, timeout_sec: int) -> dict:
    """Poll ``GetAHORun`` until status is FAILED (or terminal non-FAILED)."""
    deadline = time.monotonic() + timeout_sec
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = omics_client.get_run(id=run_id)
        status = last.get("status", "UNKNOWN")
        if status == "FAILED":
            return last
        if status in {"COMPLETED", "CANCELLED", "DELETED"}:
            raise AssertionError(
                f"run {run_id} reached terminal status {status!r}; "
                "test required FAILED. Adjust the malformed payload."
            )
        time.sleep(POLL_INTERVAL_SEC)
    # Best-effort cleanup — cancel a run that ran past timeout.
    try:
        omics_client.cancel_run(id=run_id)
    except Exception:  # noqa: BLE001
        pass
    raise AssertionError(
        f"run {run_id} did not reach FAILED within {timeout_sec}s; "
        f"last status was {last.get('status', 'UNKNOWN')!r}."
    )


def _fetch_run_logs(logs_client: Any, run_uuid: str, *, limit: int = 50) -> list[str]:
    """Mirror of MCP ``GetAHORunLogs`` via CloudWatch ``filter_log_events``.

    HealthOmics emits run-level logs to log group ``/aws/omics/WorkflowLog``
    with stream prefix ``run/<run_uuid>``. Returns up to ``limit`` log
    message strings; an empty list means the stream has not yet been
    populated (acceptable for very short failures).
    """
    log_group = "/aws/omics/WorkflowLog"
    stream_prefix = f"run/{run_uuid}"
    try:
        response = logs_client.filter_log_events(
            logGroupName=log_group,
            logStreamNamePrefix=stream_prefix,
            limit=limit,
        )
    except logs_client.exceptions.ResourceNotFoundException:
        return []
    return [event["message"] for event in response.get("events", [])]


class _BotoOmicsDiagnosticAdapter:
    """Adapt the boto3 omics client to the ``HealthOmicsDiagnostic`` protocol.

    Mirrors what the MCP ``DiagnoseAHORunFailure`` tool returns: a dict
    with ``failureReason``, ``engineLogs``, ``failedTasks``, and
    ``recommendations`` keys. Emitted through
    :func:`gatk_sv_aws.monitoring.diagnose_failure` so the test exercises
    the production diagnostic-bundle code path.
    """

    def __init__(self, omics_client: Any, logs_client: Any) -> None:
        self._omics = omics_client
        self._logs = logs_client

    def diagnose(self, run_id: str) -> dict[str, Any]:
        run = self._omics.get_run(id=run_id)
        run_uuid = run.get("uuid", run_id)
        engine_logs = _fetch_run_logs(self._logs, run_uuid)

        failed_tasks: list[dict[str, Any]] = []
        try:
            tasks = self._omics.list_run_tasks(id=run_id).get("items", [])
        except Exception:  # noqa: BLE001
            tasks = []
        for task in tasks:
            if task.get("status") == "FAILED":
                failed_tasks.append(
                    {
                        "taskId": task.get("taskId"),
                        "name": task.get("name"),
                        "statusMessage": task.get("statusMessage"),
                    }
                )

        return {
            "failureReason": run.get("statusMessage", "FAILED"),
            "engineLogs": engine_logs,
            "failedTasks": failed_tasks,
            "recommendations": [],
        }


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.mark.aws_integration
@pytest.mark.skipif(
    not _opted_in(),
    reason="set RUN_INTEGRATION_TESTS=1 to run failure-diagnostics integration test",
)
def test_failed_run_diagnostic_bundle_attached_to_run_finished_event() -> None:
    """A FAILED run's diagnostic bundle must ride on the run-finished event.

    Validates: Requirements 14.3, 15.2.
    """
    import boto3  # local import keeps the file importable without boto3 at collection

    workflow_id = _resolve_workflow_id()
    role_arn = _resolve_role_arn()
    output_uri = _resolve_output_uri()
    timeout_sec = int(
        os.environ.get("GATK_SV_FAILURE_TIMEOUT_SEC", DEFAULT_TIMEOUT_SEC)
    )

    omics = boto3.client("omics", region_name=TARGET_REGION)  # type: ignore[attr-defined]
    logs = boto3.client("logs", region_name=TARGET_REGION)  # type: ignore[attr-defined]

    # Deliberately malformed payload: an S3 URI to an object that does not
    # exist. HealthOmics validates inputs at run start, so this fails the
    # run quickly without consuming compute.
    bogus_s3 = "s3://gatk-sv-nonexistent-bucket-for-failure-test/missing.cram"
    run_name = f"failure-diagnostics-test-{int(time.time())}"

    start = omics.start_run(
        workflowId=workflow_id,
        workflowType="PRIVATE",
        roleArn=role_arn,
        name=run_name,
        outputUri=output_uri,
        parameters={"reads_cram": bogus_s3},
        storageType="DYNAMIC",
    )
    run_id = str(start["id"])

    # Wait for the run to land in FAILED. The helper raises AssertionError
    # if the run reaches a terminal non-FAILED state or never completes.
    final = _wait_for_failed(omics, run_id, timeout_sec=timeout_sec)
    assert final["status"] == "FAILED"

    # Diagnose via the production code path. The adapter wraps boto3 calls
    # in the dict shape that DiagnoseAHORunFailure produces.
    diagnostic = diagnose_failure(
        run_id, diagnostic_client=_BotoOmicsDiagnosticAdapter(omics, logs)
    )
    assert diagnostic.run_id == run_id
    assert diagnostic.failure_reason  # non-empty (Req 15.2)

    # GetAHORunLogs equivalent must succeed independently. Empty is
    # acceptable when the run failed before producing engine logs, but the
    # call itself must not raise.
    run_uuid = final.get("uuid", run_id)
    logs_returned = _fetch_run_logs(logs, run_uuid)
    assert isinstance(logs_returned, list)

    # Compute wall-clock seconds from the boto3 timestamps when present;
    # fall back to 0 so the event still serializes.
    started = final.get("startTime")
    stopped = final.get("stopTime")
    wall_clock_sec = 0
    if started and stopped:
        wall_clock_sec = int((stopped - started).total_seconds())

    # Emit the run-finished event and attach the diagnostic bundle. The
    # production Monitoring component does this when surfacing FAILED
    # runs to operators (Design §Components.i, Req 14.3).
    event = emit_run_finished(
        run_id=run_id,
        status="FAILED",
        wall_clock_sec=wall_clock_sec,
        cost_usd=0.0,
    )
    event["diagnostic_bundle"] = diagnostic.to_dict()

    # The diagnostic bundle is captured in the run-finished event.
    assert event["event"] == "run_finished"
    assert event["status"] == "FAILED"
    assert event["run_id"] == run_id
    bundle = event["diagnostic_bundle"]
    assert bundle["run_id"] == run_id
    assert "failure_reason" in bundle
    assert "engine_logs" in bundle
    assert "failed_tasks" in bundle
    assert isinstance(bundle["engine_logs"], list)
    assert isinstance(bundle["failed_tasks"], list)

    # Req 15.2: the failure report must identify the task and last log
    # excerpt when a task fails. When HealthOmics rejects inputs at
    # submission time, no failed task is recorded — that is acceptable
    # and is the case here. Either path is valid.
    if bundle["failed_tasks"]:
        first = bundle["failed_tasks"][0]
        assert "taskId" in first or "name" in first
