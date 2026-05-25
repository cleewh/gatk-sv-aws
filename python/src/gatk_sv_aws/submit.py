"""Customer-facing cohort submission and monitoring.

Provides a single entry point that:

1. Resolves the deployed Step Functions state-machine ARN (from CFN
   stack outputs).
2. Validates the cohort manifest locally (Req 6) before sending anything
   to AWS, so input errors surface in seconds, not hours.
3. Starts a state-machine execution with the manifest, output URI, and
   any optional overrides.
4. Optionally tails the execution: prints module-by-module progress,
   waits for terminal status, and prints the final cost report.

The state machine itself runs the full ten-module pipeline end-to-end
(see :mod:`gatk_sv_aws.step_functions.stack`); this module is the
synchronous customer-side wrapper.

Wired into the CLI as::

    gatk-sv-healthomics submit --manifest manifest.json \\
                              --cohort-id <ID> \\
                              --output-uri s3://bucket/prefix \\
                              [--wait]
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


DEFAULT_STACK_NAME = "GatkSvOrchestratorStack"
DEFAULT_STATE_MACHINE_OUTPUT_KEY = "StateMachineArn"
DEFAULT_REGION = "ap-southeast-1"

# Polling cadence when --wait is set.
EXECUTION_POLL_INTERVAL_SECONDS = 30

# Module names in execution order (mirror constants.MODULE_EXECUTION_ORDER
# but kept local so this module is import-light at CLI startup).
PIPELINE_MODULES = (
    "GatherSampleEvidence",
    "GatherBatchEvidence",
    "ClusterBatch",
    "GenerateBatchMetrics",
    "FilterBatch",
    "MergeBatchSites",
    "GenotypeBatch",
    "RegenotypeCNVs",
    "MakeCohortVcf",
    "AnnotateVcf",
)


@dataclass
class SubmitResult:
    """Return value from :func:`submit_cohort`."""

    execution_arn: str
    state_machine_arn: str
    cohort_id: str
    output_uri: str
    started_at: str
    region: str

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "execution_arn": self.execution_arn,
            "state_machine_arn": self.state_machine_arn,
            "cohort_id": self.cohort_id,
            "output_uri": self.output_uri,
            "started_at": self.started_at,
            "region": self.region,
        }


@dataclass
class WaitResult:
    """Return value from :func:`wait_for_completion`."""

    status: str
    duration_seconds: float
    output: dict[str, Any] | None
    error: str | None
    cause: str | None


def resolve_state_machine_arn(
    *,
    region: str,
    stack_name: str = DEFAULT_STACK_NAME,
    output_key: str = DEFAULT_STATE_MACHINE_OUTPUT_KEY,
    cf_client: Any | None = None,
    explicit_arn: str | None = None,
) -> str:
    """Find the deployed state machine ARN.

    Resolution order:
        1. ``explicit_arn`` if provided (CLI ``--state-machine-arn``).
        2. CloudFormation stack output ``output_key`` on ``stack_name``.

    Raises:
        RuntimeError if neither path produces an ARN.
    """
    if explicit_arn:
        return explicit_arn

    if cf_client is None:
        import boto3  # imported lazily so unit tests don't need boto3

        cf_client = boto3.client("cloudformation", region_name=region)

    try:
        resp = cf_client.describe_stacks(StackName=stack_name)
    except Exception as exc:
        raise RuntimeError(
            f"Could not describe stack {stack_name!r} in {region}: {exc}. "
            "Has the orchestrator been deployed? Run `cdk deploy "
            f"{stack_name}` from python/src/gatk_sv_aws/step_functions/."
        ) from exc

    stacks = resp.get("Stacks", [])
    if not stacks:
        raise RuntimeError(
            f"Stack {stack_name!r} not found in {region}. "
            "Deploy the orchestrator first."
        )

    for output in stacks[0].get("Outputs", []) or []:
        if output.get("OutputKey") == output_key:
            return output["OutputValue"]

    # Fall back: synthesize the ARN by listing state machines and matching name.
    state_machine_name = "GatkSv-Pipeline-Orchestrator"
    try:
        sfn = (cf_client.meta.client if hasattr(cf_client, "meta") else None) or None
        if sfn is None:
            import boto3

            sfn = boto3.client("stepfunctions", region_name=region)
        for page in sfn.get_paginator("list_state_machines").paginate():
            for sm in page.get("stateMachines", []):
                if sm["name"] == state_machine_name:
                    return sm["stateMachineArn"]
    except Exception:
        pass

    raise RuntimeError(
        f"Stack {stack_name!r} has no output named {output_key!r}. "
        "Re-deploy the orchestrator stack with the latest stack.py "
        "to expose the StateMachineArn output."
    )


def load_manifest_json(path: Path | str) -> dict[str, Any]:
    """Read and parse a manifest JSON from local disk.

    The state machine itself accepts either an inline manifest dict or
    an S3 URI; this helper covers the local-file case.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"manifest not found: {p}")
    return json.loads(p.read_text())


def validate_manifest_locally(manifest: dict[str, Any]) -> list[str]:
    """Run the same validation rules as the in-cluster Lambda.

    Returns a list of human-readable error messages (empty if the
    manifest is valid).
    """
    from gatk_sv_aws.models import SampleManifest
    from gatk_sv_aws.orchestrator import validate_manifest

    parsed = SampleManifest.model_validate(manifest)
    issues = validate_manifest(parsed)
    return [f"{i.sample_id}: {i.rule}: {i.detail}" for i in issues]


def _validate_cohort_id(cohort_id: str) -> None:
    """Reject cohort IDs that don't fit the cost-tag and execution-name rules."""
    if not cohort_id:
        raise ValueError("--cohort-id may not be empty")
    if len(cohort_id) > 80:
        # Step Functions execution name limit is 80 chars.
        raise ValueError(
            f"--cohort-id is too long ({len(cohort_id)} chars); max 80"
        )
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", cohort_id):
        raise ValueError(
            "--cohort-id may only contain letters, digits, dashes, and underscores"
        )


def _validate_output_uri(output_uri: str) -> None:
    if not output_uri.startswith("s3://"):
        raise ValueError(f"--output-uri must start with s3://: {output_uri!r}")


def submit_cohort(
    *,
    cohort_id: str,
    manifest: dict[str, Any] | str,
    output_uri: str,
    region: str = DEFAULT_REGION,
    state_machine_arn: str | None = None,
    stack_name: str = DEFAULT_STACK_NAME,
    overrides: dict[str, Any] | None = None,
    sfn_client: Any | None = None,
    cf_client: Any | None = None,
) -> SubmitResult:
    """Start a state-machine execution that runs the full ten-module pipeline.

    Args:
        cohort_id: Stable cohort identifier. Becomes the execution name and
            the value of every ``gatk-sv:cohort-id`` tag, so Cost Explorer
            can attribute the entire pipeline run to one number.
        manifest: Either a parsed manifest dict, or an ``s3://...`` URI.
            Local file paths can be loaded via
            :func:`load_manifest_json`.
        output_uri: ``s3://bucket/prefix`` where every module's outputs
            will be written. Must be in the target region.
        region: AWS region. Defaults to ``ap-southeast-1``.
        state_machine_arn: Override CloudFormation lookup; useful for tests.
        stack_name: CloudFormation stack to query for the state machine ARN.
        overrides: Optional ``{storage_type, cache_id, networking_mode}``
            dict that the state machine forwards to its lambdas.
        sfn_client / cf_client: Injected for tests; defaulted to ``boto3``.

    Returns:
        :class:`SubmitResult` with the execution ARN you can pass to
        :func:`wait_for_completion`.
    """
    _validate_cohort_id(cohort_id)
    _validate_output_uri(output_uri)

    arn = resolve_state_machine_arn(
        region=region,
        stack_name=stack_name,
        cf_client=cf_client,
        explicit_arn=state_machine_arn,
    )

    if isinstance(manifest, dict):
        # Run the same checks as the Lambda before paying for an execution.
        errors = validate_manifest_locally(manifest)
        if errors:
            joined = "\n  ".join(errors)
            raise ValueError(
                f"manifest validation failed before submission:\n  {joined}"
            )

    payload: dict[str, Any] = {
        "cohort_id": cohort_id,
        "sample_manifest": manifest,
        "output_uri": output_uri,
    }
    if overrides:
        payload["overrides"] = overrides

    if sfn_client is None:
        import boto3

        sfn_client = boto3.client("stepfunctions", region_name=region)

    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    resp = sfn_client.start_execution(
        stateMachineArn=arn,
        name=cohort_id,
        input=json.dumps(payload),
    )
    return SubmitResult(
        execution_arn=resp["executionArn"],
        state_machine_arn=arn,
        cohort_id=cohort_id,
        output_uri=output_uri,
        started_at=started_at,
        region=region,
    )


def wait_for_completion(
    *,
    execution_arn: str,
    region: str = DEFAULT_REGION,
    poll_interval_seconds: int = EXECUTION_POLL_INTERVAL_SECONDS,
    sfn_client: Any | None = None,
    progress_callback: Any | None = None,
) -> WaitResult:
    """Block until the state-machine execution reaches a terminal status.

    Args:
        execution_arn: ARN returned by :func:`submit_cohort`.
        region: AWS region.
        poll_interval_seconds: Time between ``DescribeExecution`` polls.
        sfn_client: Injected for tests.
        progress_callback: Optional callable invoked with each poll's
            ``DescribeExecution`` response. Lets the CLI print module-
            by-module progress without baking that into this function.

    Returns:
        :class:`WaitResult`. ``status`` is one of ``SUCCEEDED``,
        ``FAILED``, ``TIMED_OUT``, or ``ABORTED``.
    """
    if sfn_client is None:
        import boto3

        sfn_client = boto3.client("stepfunctions", region_name=region)

    started = time.monotonic()
    while True:
        resp = sfn_client.describe_execution(executionArn=execution_arn)
        status = resp.get("status", "RUNNING")
        if progress_callback is not None:
            try:
                progress_callback(resp)
            except Exception:
                logger.exception("progress_callback raised; continuing poll loop")

        if status in {"SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"}:
            duration = time.monotonic() - started
            output_str = resp.get("output")
            output = None
            if isinstance(output_str, str) and output_str:
                try:
                    output = json.loads(output_str)
                except json.JSONDecodeError:
                    output = {"raw": output_str}
            return WaitResult(
                status=status,
                duration_seconds=duration,
                output=output,
                error=resp.get("error"),
                cause=resp.get("cause"),
            )

        time.sleep(poll_interval_seconds)


def format_progress(resp: dict[str, Any]) -> str:
    """Return a one-line summary suitable for ``print()`` from a progress callback."""
    name = resp.get("name", "<unknown>")
    status = resp.get("status", "<unknown>")
    started_at = resp.get("startDate", "")
    return f"[{started_at}] execution={name} status={status}"
