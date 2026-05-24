"""Lambda handler: start-run.

Submits a single HealthOmics workflow run with cache configuration and
cost-tracking tags. Reads the HealthOmics role ARN and cache ID from
environment variables (never from user input).

Requirements: 6.1, 6.2, 7.3, 10.3, 12.4.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import boto3

from gatk_sv_aws.step_functions import constants
from gatk_sv_aws.step_functions.logging_config import (
    configure_lambda_logging,
)
from gatk_sv_aws.step_functions.models import (
    StartRunInput,
    StartRunOutput,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

omics_client = boto3.client("omics")


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Submit a HealthOmics workflow run and return run metadata.

    Parameters
    ----------
    event : dict
        Lambda input containing module, workflow_id, workflow_version_name,
        parameters, output_uri, cohort_id, sample_count, attempt_number.
    context : Any
        Lambda context object (unused).

    Returns
    -------
    dict
        Run submission result with run_id, arn, status, module,
        attempt_number.
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
        input_model = StartRunInput.model_validate(event)
    except Exception as exc:
        logger.error("Input validation failed", extra={"error": str(exc)})
        raise ValueError(f"Invalid start-run input: {exc}") from exc

    # Read configuration from environment (Req 12.4: never from user input)
    role_arn = os.environ.get("HEALTHOMICS_ROLE_ARN", constants.DEFAULT_ROLE_ARN)
    cache_id = os.environ.get("CACHE_ID", constants.DEFAULT_CACHE_ID)

    # Build run name
    run_name = (
        f"{input_model.cohort_id}-{input_model.module}"
        f"-attempt{input_model.attempt_number}"
    )

    # Build cost-tracking tags (Req 7.3)
    tags = {
        "gatk-sv:cohort-id": input_model.cohort_id,
        "gatk-sv:workflow-version": input_model.workflow_version_name,
        "gatk-sv:module": input_model.module,
        "gatk-sv:sample-count": str(input_model.sample_count),
    }

    logger.info(
        "Submitting HealthOmics run",
        extra={
            "cohort_id": input_model.cohort_id,
            "current_module": input_model.module,
            "attempt_number": input_model.attempt_number,
            "workflow_id": input_model.workflow_id,
            "run_name": run_name,
        },
    )

    try:
        response = omics_client.start_run(
            workflowId=input_model.workflow_id,
            workflowType="PRIVATE",
            roleArn=role_arn,
            name=run_name,
            outputUri=input_model.output_uri,
            parameters=input_model.parameters,
            storageType="DYNAMIC",
            cacheId=cache_id,
            cacheBehavior="CACHE_ALWAYS",
            tags=tags,
        )
    except Exception as exc:
        logger.error(
            "Failed to start HealthOmics run",
            extra={
                "cohort_id": input_model.cohort_id,
                "current_module": input_model.module,
                "error": str(exc),
            },
        )
        raise

    run_id = response["id"]
    arn = response.get("arn", "")
    status = response.get("status", "PENDING")

    logger.info(
        "HealthOmics run submitted",
        extra={
            "cohort_id": input_model.cohort_id,
            "current_module": input_model.module,
            "run_id": run_id,
            "arn": arn,
            "run_status": status,
        },
    )

    output = StartRunOutput(
        run_id=run_id,
        arn=arn,
        status=status,
        module=input_model.module,
        attempt_number=input_model.attempt_number,
    )
    return output.model_dump()
