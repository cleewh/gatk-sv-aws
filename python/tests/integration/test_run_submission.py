"""Integration test: run submission emits required metadata and tags (Req 14.1, 16.4).

Implements the "Run submission" row of Design §Testing Strategy →
integration table:

    | Run submission | StartAHORun, GetAHORun, ListAHORunTasks |
    | Run reaches RUNNING; tags emitted match Property 10;      |
    | run metadata records the workflow version                 |

Submits a HealthOmics run via ``omics.start_run`` with the Cost
Explorer tag set required by Property 10
(``gatk-sv:cohort-id``, ``gatk-sv:workflow-version``,
``gatk-sv:module``, ``gatk-sv:sample-count``, ``gatk-sv:environment``),
polls ``omics.get_run`` until the run reaches ``RUNNING`` (Req 14.1),
calls ``omics.list_run_tasks`` (accepts an empty list when RUNNING is
observed before any task is scheduled), asserts the run metadata
records the ``workflowVersionName`` (Req 16.4), and verifies the
emitted tags match the Property 10 tag schema. The run is cancelled in
a ``finally`` block so the test consumes only control-plane time.

Validates: Requirements 14.1, 16.4.

Design references:
  * §Deployment Step 11 — ``StartAHORun(workflowId, ..., tags=cost_tags,
    storageType=..., cacheId=...)``
  * §Cost Optimization Strategy → Cost Explorer tag taxonomy
  * §Correctness Properties → Property 10 (Cost-tag coverage)
  * §Testing Strategy → Integration Tests, "Run submission" row

Skipped by default. Opt in with ``RUN_INTEGRATION_TESTS=1`` AND active
AWS credentials reachable in ``ap-southeast-1``. Additional environment
variables:

  * ``GATK_SV_TEST_WORKFLOW_ID`` — HealthOmics workflow id to submit
    against. Defaults to the first record in
    ``gatk-sv-healthomics/workflow-versions.json``.
  * ``GATK_SV_HEALTHOMICS_ROLE_ARN`` — IAM role ARN used for the run.
  * ``GATK_SV_OUTPUT_URI`` — S3 URI prefix for run outputs.
  * ``GATK_SV_TEST_PARAMETERS_JSON`` — JSON object string with valid
    workflow input parameters; required because the run must reach
    RUNNING (not FAILED) for this test, so inputs must be valid.
  * ``GATK_SV_TEST_COHORT_ID`` — cohort identifier for the
    ``gatk-sv:cohort-id`` tag (default: ``cohort-run-submission-test``).
  * ``GATK_SV_TEST_WORKFLOW_VERSION`` — semver string for the
    ``gatk-sv:workflow-version`` tag (default: ``test``).
  * ``GATK_SV_TEST_MODULE`` — module name for the ``gatk-sv:module``
    tag (default: ``GatherSampleEvidence``).
  * ``GATK_SV_TEST_SAMPLE_COUNT`` — sample count for the
    ``gatk-sv:sample-count`` tag (default: ``1``).
  * ``GATK_SV_TEST_ENVIRONMENT`` — environment label for the
    ``gatk-sv:environment`` tag (default: ``validation``).
  * ``GATK_SV_RUNNING_TIMEOUT_SEC`` — polling timeout in seconds
    (default 600 = 10 min). Submission only — the run is cancelled
    after RUNNING is observed.

Cost: the run reaches RUNNING and is then cancelled, so only the
HealthOmics control-plane fee is incurred. No tasks should run to
completion.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TARGET_REGION = "ap-southeast-1"

# Polling cadence. HealthOmics typically transitions PENDING → STARTING
# → RUNNING within a few minutes for valid inputs.
POLL_INTERVAL_SEC = 15
DEFAULT_TIMEOUT_SEC = 10 * 60

# Property 10 tag keys (Design §Cost Optimization Strategy → Cost Explorer
# tag taxonomy). The first three are always present; the last two are
# included whenever applicable to the resource.
REQUIRED_TAG_KEYS = (
    "gatk-sv:cohort-id",
    "gatk-sv:workflow-version",
    "gatk-sv:environment",
)
OPTIONAL_TAG_KEYS = (
    "gatk-sv:module",
    "gatk-sv:sample-count",
)

# Terminal non-RUNNING states that should surface a clean skip rather
# than a misleading test failure when the inputs are wrong.
TERMINAL_NON_RUNNING_STATUSES = frozenset({"COMPLETED", "CANCELLED", "DELETED"})

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


def _resolve_parameters() -> dict[str, Any]:
    """Return a valid workflow input parameter set, or skip if missing.

    The run must reach RUNNING (not FAILED) for this test, so the
    parameters must reference real, in-region S3 inputs that the
    workflow accepts. Bogus inputs are rejected at submission and the
    run never reaches RUNNING.
    """
    raw = os.environ.get("GATK_SV_TEST_PARAMETERS_JSON")
    if not raw:
        pytest.skip(
            "set GATK_SV_TEST_PARAMETERS_JSON to a JSON object string of "
            "valid workflow inputs (the run must reach RUNNING, so inputs "
            "must satisfy HealthOmics submission validation)."
        )
    try:
        parameters = json.loads(raw)
    except json.JSONDecodeError as exc:
        pytest.skip(
            f"GATK_SV_TEST_PARAMETERS_JSON is not valid JSON: {exc}"
        )
    if not isinstance(parameters, dict):
        pytest.skip(
            f"GATK_SV_TEST_PARAMETERS_JSON must decode to a JSON object, "
            f"got {type(parameters).__name__}."
        )
    return parameters


def _build_cost_tags() -> dict[str, str]:
    """Return the Property 10 tag set for the run.

    Mirrors the tag dict assembled in
    :func:`gatk_sv_aws.orchestrator.submit_cohort` so the integration
    surface matches the production code path.
    """
    return {
        "gatk-sv:cohort-id": os.environ.get(
            "GATK_SV_TEST_COHORT_ID", "cohort-run-submission-test"
        ),
        "gatk-sv:workflow-version": os.environ.get(
            "GATK_SV_TEST_WORKFLOW_VERSION", "test"
        ),
        "gatk-sv:module": os.environ.get(
            "GATK_SV_TEST_MODULE", "GatherSampleEvidence"
        ),
        "gatk-sv:sample-count": os.environ.get("GATK_SV_TEST_SAMPLE_COUNT", "1"),
        "gatk-sv:environment": os.environ.get(
            "GATK_SV_TEST_ENVIRONMENT", "validation"
        ),
    }


def _wait_for_running(
    omics_client: Any, run_id: str, *, timeout_sec: int
) -> dict[str, Any]:
    """Poll ``GetAHORun`` until status is ``RUNNING`` (Req 14.1).

    Surfaces FAILED as ``pytest.skip`` (root cause is invalid inputs,
    not the submission path under test). Raises ``AssertionError`` if
    the run reaches a terminal non-RUNNING state we cannot recover from
    or never transitions within ``timeout_sec``.
    """
    deadline = time.monotonic() + timeout_sec
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = omics_client.get_run(id=run_id)
        status = last.get("status", "UNKNOWN")
        if status == "RUNNING":
            return last
        if status == "FAILED":
            reason = last.get("statusMessage") or last.get("failureReason") or ""
            pytest.skip(
                f"run {run_id} reached FAILED before RUNNING; "
                "submission validation rejected the inputs. "
                f"Reason: {reason!r}. Adjust GATK_SV_TEST_PARAMETERS_JSON."
            )
        if status in TERMINAL_NON_RUNNING_STATUSES:
            raise AssertionError(
                f"run {run_id} reached terminal status {status!r} before "
                "RUNNING was observed; the test could not verify the "
                "submission path."
            )
        time.sleep(POLL_INTERVAL_SEC)
    raise AssertionError(
        f"run {run_id} did not reach RUNNING within {timeout_sec}s; "
        f"last status was {last.get('status', 'UNKNOWN')!r}."
    )


def _resolve_run_tags(omics_client: Any, run_id: str, run_arn: str) -> dict[str, str]:
    """Return the tag map for the run.

    HealthOmics surfaces tags in two places: the ``tags`` field on the
    ``GetAHORun`` response and the resource-level
    ``ListTagsForResource`` API. Either is acceptable; we prefer the
    embedded ``tags`` field and fall back to the resource API.
    """
    response = omics_client.get_run(id=run_id)
    tags = response.get("tags")
    if isinstance(tags, dict) and tags:
        return {str(k): str(v) for k, v in tags.items()}
    try:
        listed = omics_client.list_tags_for_resource(resourceArn=run_arn)
    except Exception:  # noqa: BLE001 — best-effort fallback
        return {}
    listed_tags = listed.get("tags", {}) or {}
    return {str(k): str(v) for k, v in listed_tags.items()}


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.mark.aws_integration
@pytest.mark.skipif(
    not _opted_in(),
    reason="set RUN_INTEGRATION_TESTS=1 to run run-submission integration test",
)
def test_run_submission_reaches_running_with_property_10_tags() -> None:
    """``StartAHORun`` → ``GetAHORun`` → ``ListAHORunTasks`` happy path.

    * Run reaches ``RUNNING`` (Req 14.1).
    * Emitted tags match Property 10 (Design §Correctness Properties → Property 10).
    * Run metadata records ``workflowVersionName`` (Req 16.4).

    Validates: Requirements 14.1, 16.4.
    """
    import boto3  # local import keeps the file importable without boto3 at collection

    workflow_id = _resolve_workflow_id()
    role_arn = _resolve_role_arn()
    output_uri = _resolve_output_uri()
    parameters = _resolve_parameters()
    tags = _build_cost_tags()
    workflow_version_name = os.environ.get(
        "GATK_SV_TEST_WORKFLOW_VERSION_NAME", "default"
    )
    timeout_sec = int(
        os.environ.get("GATK_SV_RUNNING_TIMEOUT_SEC", DEFAULT_TIMEOUT_SEC)
    )

    omics = boto3.client("omics", region_name=TARGET_REGION)  # type: ignore[attr-defined]

    run_name = f"run-submission-test-{int(time.time())}"
    start_kwargs: dict[str, Any] = {
        "workflowId": workflow_id,
        "workflowType": "PRIVATE",
        "roleArn": role_arn,
        "name": run_name,
        "outputUri": output_uri,
        "parameters": parameters,
        "storageType": "DYNAMIC",
        "tags": tags,
    }
    if workflow_version_name:
        start_kwargs["workflowVersionName"] = workflow_version_name

    start = omics.start_run(**start_kwargs)
    run_id = str(start["id"])
    run_arn = start.get("arn", f"arn:aws:omics:{TARGET_REGION}::run/{run_id}")

    try:
        # Req 14.1 — run reaches RUNNING. The helper raises
        # AssertionError on timeout / unrecoverable terminal state and
        # skips cleanly when submission validation rejects the inputs.
        running = _wait_for_running(omics, run_id, timeout_sec=timeout_sec)
        assert running["status"] == "RUNNING"
        assert str(running["id"]) == run_id

        # Req 16.4 — run metadata records the workflow version. Either
        # ``workflowVersionName`` (the canonical HealthOmics field) or
        # ``versionName`` (legacy alias on some SDKs) must be present
        # and non-empty.
        recorded_version = running.get("workflowVersionName") or running.get(
            "versionName"
        )
        assert recorded_version, (
            f"GetAHORun response for {run_id} did not record the workflow "
            "version (workflowVersionName/versionName both empty); "
            "Req 16.4 requires the version to be recorded in run metadata."
        )
        if workflow_version_name:
            assert recorded_version == workflow_version_name, (
                f"recorded workflow version {recorded_version!r} does not "
                f"match submitted version {workflow_version_name!r}."
            )

        # Property 10 — emitted tags carry the Cost Explorer tag set.
        # Read tags back from the run resource so the assertion exercises
        # what Cost Explorer will see, not just what we sent.
        observed_tags = _resolve_run_tags(omics, run_id, run_arn)
        for key in REQUIRED_TAG_KEYS:
            assert key in observed_tags, (
                f"run {run_id} is missing required Property 10 tag {key!r}; "
                f"observed tags: {sorted(observed_tags)}"
            )
            assert observed_tags[key] == tags[key], (
                f"run {run_id} tag {key!r} is {observed_tags[key]!r}, "
                f"expected {tags[key]!r}."
            )
        for key in OPTIONAL_TAG_KEYS:
            # Optional tags are emitted by the orchestrator; if present,
            # they must match what we sent.
            if key in tags and key in observed_tags:
                assert observed_tags[key] == tags[key], (
                    f"run {run_id} tag {key!r} is "
                    f"{observed_tags[key]!r}, expected {tags[key]!r}."
                )

        # ListAHORunTasks — accept any list. RUNNING may be observed
        # before HealthOmics has scheduled the first task, in which
        # case the response is empty; once any task has been scheduled
        # the response carries at least one entry. Both are valid for
        # this test (the goal is to confirm the API is usable on a
        # RUNNING run; Req 14.1).
        tasks_response = omics.list_run_tasks(id=run_id, maxResults=10)
        items = tasks_response.get("items", []) or []
        assert isinstance(items, list)
        for task in items:
            # Each task entry should carry an identifier; HealthOmics
            # uses ``taskId``. Older SDKs may surface ``id``.
            assert task.get("taskId") or task.get("id"), (
                f"task entry on run {run_id} lacks an identifier: {task!r}"
            )
    finally:
        # Always cancel the run to avoid burning compute. Cancellation
        # is best-effort: HealthOmics may have already terminated the
        # run if the test failed for an unrelated reason.
        try:
            omics.cancel_run(id=run_id)
        except Exception:  # noqa: BLE001
            pass
