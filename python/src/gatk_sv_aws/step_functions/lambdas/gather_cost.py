"""Lambda handler: gather-cost.

Collects cost data from all completed module runs and produces the final
cost report. Writes cost-report.json to the cohort's output S3 prefix.

Requirements: 7.1, 7.2, 7.3, 7.4, 10.3.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import boto3

from gatk_sv_aws.step_functions.logging_config import (
    configure_lambda_logging,
)
from gatk_sv_aws.step_functions.models import (
    CostReport,
    CostReportEntry,
    GatherCostInput,
    GatherCostOutput,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

omics_client = boto3.client("omics")
s3_client = boto3.client("s3")

# Placeholder cost rate: USD per second of run time.
# HealthOmics does not directly expose cost in GetRun; this is a simplified
# estimate until real pricing integration is available.
_COST_RATE_USD_PER_SECOND = 0.001


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    """Parse an S3 URI into (bucket, key)."""
    if not uri.startswith("s3://"):
        raise ValueError(f"Invalid S3 URI: {uri!r}")
    path = uri[5:]
    bucket, _, key = path.partition("/")
    if not bucket or not key:
        raise ValueError(f"Invalid S3 URI (missing bucket or key): {uri!r}")
    return bucket, key


def _build_cost_report_uri(output_uri: str) -> str:
    """Construct the cost report S3 URI with exactly one slash separator.

    Handles output_uri with or without trailing slash (Property 7).
    """
    return output_uri.rstrip("/") + "/cost-report.json"


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Gather cost data and produce the cost report.

    Parameters
    ----------
    event : dict
        Lambda input containing cohort_id, sample_count, output_uri,
        and module_runs list.
    context : Any
        Lambda context object (unused).

    Returns
    -------
    dict
        Cost report result with cost_report and cost_report_uri.
    """
    # --- Task 6.5: Configure structured logging with execution context ---
    global logger
    logger = configure_lambda_logging(
        cohort_id=event.get("cohort_id", ""),
        module="GatherCost",
        attempt_number=1,
        logger_name=__name__,
    )

    try:
        input_model = GatherCostInput.model_validate(event)
    except Exception as exc:
        logger.error("Input validation failed", extra={"error": str(exc)})
        raise ValueError(f"Invalid gather-cost input: {exc}") from exc

    logger.info(
        "Gathering cost data",
        extra={
            "cohort_id": input_model.cohort_id,
            "sample_count": input_model.sample_count,
            "module_count": len(input_model.module_runs),
        },
    )

    # Collect cost data for each module run
    module_entries: list[CostReportEntry] = []
    total_cost_usd = 0.0

    for module_run in input_model.module_runs:
        run_id = module_run["run_id"]
        module = module_run["module"]
        is_cache_hit = module_run.get("is_cache_hit", False)

        duration_seconds = 0
        cost_usd = 0.0

        try:
            response = omics_client.get_run(id=run_id)
            start_time = response.get("startTime")
            stop_time = response.get("stopTime")

            if start_time and stop_time:
                duration_seconds = int((stop_time - start_time).total_seconds())

            # Compute cost: cache hits are free, otherwise use duration * rate
            if is_cache_hit:
                cost_usd = 0.0
            else:
                cost_usd = duration_seconds * _COST_RATE_USD_PER_SECOND

        except Exception as exc:
            logger.warning(
                "Failed to get run details for cost calculation",
                extra={
                    "cohort_id": input_model.cohort_id,
                    "current_module": module,
                    "run_id": run_id,
                    "error": str(exc),
                },
            )
            # Use zero cost if we can't retrieve run details
            duration_seconds = 0
            cost_usd = 0.0

        total_cost_usd += cost_usd
        module_entries.append(
            CostReportEntry(
                module=module,
                run_id=run_id,
                cost_usd=cost_usd,
                duration_seconds=duration_seconds,
                is_cache_hit=is_cache_hit,
            )
        )

    # Compute per-sample cost
    per_sample_cost_usd = total_cost_usd / input_model.sample_count

    # Build cost report
    generated_at = datetime.now(tz=timezone.utc).isoformat()
    cost_report = CostReport(
        cohort_id=input_model.cohort_id,
        sample_count=input_model.sample_count,
        total_cost_usd=total_cost_usd,
        per_sample_cost_usd=per_sample_cost_usd,
        modules=module_entries,
        generated_at=generated_at,
    )

    # Write cost report to S3
    cost_report_uri = _build_cost_report_uri(input_model.output_uri)

    try:
        bucket, key = _parse_s3_uri(cost_report_uri)
        report_json = cost_report.model_dump_json(indent=2)
        s3_client.put_object(
            Bucket=bucket,
            Key=key,
            Body=report_json.encode("utf-8"),
            ContentType="application/json",
        )
        logger.info(
            "Cost report written to S3",
            extra={
                "cohort_id": input_model.cohort_id,
                "cost_report_uri": cost_report_uri,
                "total_cost_usd": total_cost_usd,
                "per_sample_cost_usd": per_sample_cost_usd,
            },
        )
    except Exception as exc:
        logger.error(
            "Failed to write cost report to S3",
            extra={
                "cohort_id": input_model.cohort_id,
                "cost_report_uri": cost_report_uri,
                "error": str(exc),
            },
        )
        raise

    output = GatherCostOutput(
        cost_report=cost_report,
        cost_report_uri=cost_report_uri,
    )
    return output.model_dump()
