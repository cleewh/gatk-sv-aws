"""Lambda handler: poll-status.

Checks the status of a HealthOmics run and returns structured status
information for the state machine's Choice state. Emits CloudWatch
metrics and EventBridge events on terminal states. Detects module
timeouts (24-hour limit).

Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 10.1, 10.2, 10.3, 11.2.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import boto3

from gatk_sv_aws.step_functions.constants import (
    MODULE_TIMEOUT_SECONDS,
)
from gatk_sv_aws.step_functions.logging_config import (
    configure_lambda_logging,
)
from gatk_sv_aws.step_functions.models import (
    PollStatusInput,
    PollStatusOutput,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

omics_client = boto3.client("omics")
cloudwatch_client = boto3.client("cloudwatch")
events_client = boto3.client("events")

# Terminal statuses for HealthOmics runs
_TERMINAL_STATUSES = frozenset({"COMPLETED", "FAILED", "CANCELLED"})

# Cache hit heuristic: runs completing in under 60 seconds are likely cache hits
_CACHE_HIT_THRESHOLD_SECONDS = 60

# CloudWatch namespace for orchestrator metrics
_METRICS_NAMESPACE = "GatkSv/Orchestrator"

# EventBridge source for module events
_EVENT_SOURCE = "gatk-sv.orchestrator"


def _emit_cloudwatch_metrics(
    module: str,
    status: str,
    duration_seconds: int | None,
    estimated_cost: float | None,
) -> None:
    """Emit CloudWatch custom metrics for terminal module states.

    Wrapped in try/except so observability failures don't break the Lambda.
    """
    try:
        metric_data: list[dict[str, Any]] = []

        if status == "COMPLETED":
            metric_data.append(
                {
                    "MetricName": "ModulesCompleted",
                    "Value": 1,
                    "Unit": "Count",
                    "Dimensions": [{"Name": "Module", "Value": module}],
                }
            )
            if duration_seconds is not None:
                metric_data.append(
                    {
                        "MetricName": "ModuleDuration",
                        "Value": float(duration_seconds),
                        "Unit": "Seconds",
                        "Dimensions": [{"Name": "Module", "Value": module}],
                    }
                )
            if estimated_cost is not None:
                metric_data.append(
                    {
                        "MetricName": "ModuleCost",
                        "Value": estimated_cost,
                        "Unit": "None",
                        "Dimensions": [{"Name": "Module", "Value": module}],
                    }
                )
        elif status == "FAILED":
            metric_data.append(
                {
                    "MetricName": "ModulesFailed",
                    "Value": 1,
                    "Unit": "Count",
                    "Dimensions": [{"Name": "Module", "Value": module}],
                }
            )

        if metric_data:
            cloudwatch_client.put_metric_data(
                Namespace=_METRICS_NAMESPACE,
                MetricData=metric_data,
            )
    except Exception as exc:
        logger.warning(
            "Failed to emit CloudWatch metrics (non-fatal)",
            extra={"error": str(exc), "current_module": module},
        )


def _publish_eventbridge_event(
    cohort_id: str,
    module: str,
    status: str,
    run_id: str,
    duration_seconds: int | None,
) -> None:
    """Publish an EventBridge event on module terminal state transitions.

    Wrapped in try/except so observability failures don't break the Lambda.
    """
    try:
        detail = {
            "cohort_id": cohort_id,
            "module": module,
            "status": status,
            "run_id": run_id,
            "duration_seconds": duration_seconds,
        }
        events_client.put_events(
            Entries=[
                {
                    "Source": _EVENT_SOURCE,
                    "DetailType": "ModuleStatusChange",
                    "Detail": json.dumps(detail),
                }
            ]
        )
    except Exception as exc:
        logger.warning(
            "Failed to publish EventBridge event (non-fatal)",
            extra={"error": str(exc), "current_module": module},
        )


def _check_module_timeout(module_start_time: str | None) -> bool:
    """Check if the module has exceeded the 24-hour timeout.

    Parameters
    ----------
    module_start_time : str | None
        ISO 8601 timestamp of when the module was first submitted.

    Returns
    -------
    bool
        True if the module has timed out, False otherwise.
    """
    if not module_start_time:
        return False

    try:
        start_dt = datetime.fromisoformat(module_start_time)
        # Ensure timezone-aware comparison
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
        now = datetime.now(tz=timezone.utc)
        elapsed = (now - start_dt).total_seconds()
        return elapsed > MODULE_TIMEOUT_SECONDS
    except (ValueError, TypeError):
        return False


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Poll HealthOmics run status and return structured result.

    Parameters
    ----------
    event : dict
        Lambda input containing run_id, module, cohort_id, attempt_number,
        and optional module_start_time.
    context : Any
        Lambda context object (unused).

    Returns
    -------
    dict
        Poll result with run_id, status, output_uri, is_terminal,
        is_cache_hit, failure_reason, error_code, duration_seconds.
    """
    # --- Task 6.5: Configure structured logging with execution context ---
    global logger
    logger = configure_lambda_logging(
        cohort_id=event.get("cohort_id", ""),
        module=event.get("module", ""),
        attempt_number=event.get("attempt_number", 1),
        logger_name=__name__,
    )

    try:
        input_model = PollStatusInput.model_validate(event)
    except Exception as exc:
        logger.error("Input validation failed", extra={"error": str(exc)})
        raise ValueError(f"Invalid poll-status input: {exc}") from exc

    logger.info(
        "Polling HealthOmics run status",
        extra={
            "cohort_id": input_model.cohort_id,
            "current_module": input_model.module,
            "run_id": input_model.run_id,
            "attempt_number": input_model.attempt_number,
        },
    )

    # --- Task 8.2: Check module timeout (24-hour guard) ---
    if _check_module_timeout(input_model.module_start_time):
        logger.warning(
            "Module timeout detected (exceeded 24 hours)",
            extra={
                "cohort_id": input_model.cohort_id,
                "current_module": input_model.module,
                "run_id": input_model.run_id,
                "module_start_time": input_model.module_start_time,
            },
        )
        output = PollStatusOutput(
            run_id=input_model.run_id,
            status="MODULE_TIMEOUT",
            output_uri=None,
            is_terminal=True,
            is_cache_hit=False,
            failure_reason="Module exceeded 24-hour timeout limit",
            error_code="ModuleTimeout",
            duration_seconds=MODULE_TIMEOUT_SECONDS,
        )
        return output.model_dump()

    try:
        response = omics_client.get_run(id=input_model.run_id)
    except Exception as exc:
        logger.error(
            "Failed to get run status",
            extra={
                "cohort_id": input_model.cohort_id,
                "current_module": input_model.module,
                "run_id": input_model.run_id,
                "error": str(exc),
            },
        )
        raise

    status = response.get("status", "UNKNOWN")
    is_terminal = status in _TERMINAL_STATUSES

    # Extract output URI (populated on COMPLETED)
    output_uri: str | None = None
    if status == "COMPLETED":
        output_uri = response.get("runOutputUri")

    # Extract failure reason (populated on FAILED)
    failure_reason: str | None = None
    error_code: str | None = None
    if status == "FAILED":
        failure_reason = response.get("statusMessage") or response.get("failureReason")
        error_code = _extract_error_code(failure_reason)

    # Compute duration
    duration_seconds: int | None = None
    start_time = response.get("startTime")
    stop_time = response.get("stopTime")
    if start_time and stop_time:
        duration_seconds = int((stop_time - start_time).total_seconds())

    # Determine cache hit: short duration + COMPLETED suggests cache hit
    is_cache_hit = False
    if status == "COMPLETED" and duration_seconds is not None:
        is_cache_hit = duration_seconds < _CACHE_HIT_THRESHOLD_SECONDS

    # --- Task 8.1: Emit CloudWatch metrics and EventBridge events on terminal states ---
    if is_terminal and status in ("COMPLETED", "FAILED"):
        # Estimate cost for metrics (simplified: duration * rate)
        estimated_cost: float | None = None
        if duration_seconds is not None and not is_cache_hit:
            estimated_cost = duration_seconds * 0.001  # USD per second placeholder

        _emit_cloudwatch_metrics(
            module=input_model.module,
            status=status,
            duration_seconds=duration_seconds,
            estimated_cost=estimated_cost,
        )
        _publish_eventbridge_event(
            cohort_id=input_model.cohort_id,
            module=input_model.module,
            status=status,
            run_id=input_model.run_id,
            duration_seconds=duration_seconds,
        )

    logger.info(
        "Run status polled",
        extra={
            "cohort_id": input_model.cohort_id,
            "current_module": input_model.module,
            "run_id": input_model.run_id,
            "run_status": status,
            "is_terminal": is_terminal,
            "is_cache_hit": is_cache_hit,
            "duration_seconds": duration_seconds,
        },
    )

    output = PollStatusOutput(
        run_id=input_model.run_id,
        status=status,
        output_uri=output_uri,
        is_terminal=is_terminal,
        is_cache_hit=is_cache_hit,
        failure_reason=failure_reason,
        error_code=error_code,
        duration_seconds=duration_seconds,
    )
    return output.model_dump()


def _extract_error_code(message: str | None) -> str | None:
    """Extract an error code from a HealthOmics status message.

    Attempts to parse known error patterns from the failure message.
    Returns the error code string if found, None otherwise.
    """
    if not message:
        return None

    # Common HealthOmics error patterns
    known_codes = [
        "InternalServerError",
        "Throttling",
        "ServiceUnavailable",
        "OutOfMemoryError",
        "InvalidParameterException",
        "ResourceNotFoundException",
        "AccessDeniedException",
    ]
    for code in known_codes:
        if code in message:
            return code

    # If no known code found, return the first word as a generic code
    # (HealthOmics often prefixes messages with the error type)
    parts = message.split(":", 1)
    if len(parts) > 1 and parts[0].strip().replace(" ", "").isalpha():
        return parts[0].strip()

    return None
