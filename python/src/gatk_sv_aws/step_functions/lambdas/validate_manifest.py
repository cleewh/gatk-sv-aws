"""Lambda handler: validate-manifest.

Validates the sample manifest as the first step in the Step Functions
state machine. Provides fast feedback on input errors before any
HealthOmics runs are submitted.

Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 9.1, 9.4, 10.3.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import boto3

from gatk_sv_aws.models import SampleManifest
from gatk_sv_aws.orchestrator import (
    validate_manifest as _validate_manifest,
)
from gatk_sv_aws.step_functions.logging_config import (
    configure_lambda_logging,
)
from gatk_sv_aws.step_functions.models import (
    ManifestValidationInput,
    ManifestValidationOutput,
    StateMachineInput,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

s3_client = boto3.client("s3")


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    """Parse an S3 URI into (bucket, key)."""
    if not uri.startswith("s3://"):
        raise ValueError(f"Invalid S3 URI: {uri!r}")
    path = uri[5:]
    bucket, _, key = path.partition("/")
    if not bucket or not key:
        raise ValueError(f"Invalid S3 URI (missing bucket or key): {uri!r}")
    return bucket, key


def _fetch_manifest_from_s3(uri: str) -> dict[str, Any]:
    """Fetch and parse a JSON manifest from S3."""
    bucket, key = _parse_s3_uri(uri)
    response = s3_client.get_object(Bucket=bucket, Key=key)
    body = response["Body"].read().decode("utf-8")
    return json.loads(body)


def _get_bucket_region(bucket: str) -> str:
    """Resolve the region of an S3 bucket."""
    response = s3_client.get_bucket_location(Bucket=bucket)
    # LocationConstraint is None for us-east-1
    location = response.get("LocationConstraint")
    return location if location else "us-east-1"


def _region_resolver(uri: str) -> str:
    """Resolve the region of an S3 URI by looking up its bucket location."""
    bucket, _ = _parse_s3_uri(uri)
    return _get_bucket_region(bucket)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Validate the sample manifest and return validation results.

    Parameters
    ----------
    event : dict
        Lambda input containing cohort_id, sample_manifest, output_uri,
        and target_region.
    context : Any
        Lambda context object (unused).

    Returns
    -------
    dict
        Validation result with validation_status, sample_count, errors,
        and resolved manifest.
    """
    # --- Task 6.5: Configure structured logging with execution context ---
    global logger
    cohort_id = event.get("cohort_id", "")
    logger = configure_lambda_logging(
        cohort_id=cohort_id,
        module="ValidateManifest",
        attempt_number=1,
        logger_name=__name__,
    )

    # --- Task 6.1: Explicit state machine input schema validation FIRST ---
    # Validate required fields (cohort_id, sample_manifest, output_uri) before
    # any other processing. This provides clear error messages for missing fields.
    try:
        StateMachineInput.model_validate(
            {
                "cohort_id": event.get("cohort_id"),
                "sample_manifest": event.get("sample_manifest"),
                "output_uri": event.get("output_uri"),
                # Pass overrides if present (optional field)
                **({"overrides": event["overrides"]} if "overrides" in event else {}),
            }
        )
    except Exception as exc:
        logger.error(
            "State machine input schema validation failed",
            extra={"error": str(exc)},
        )
        return ManifestValidationOutput(
            validation_status="FAILED",
            sample_count=0,
            errors=[{"sample_id": "", "rule": "input_schema", "detail": str(exc)}],
            manifest=None,
        ).model_dump()

    # --- Continue with handler-specific validation ---
    try:
        input_model = ManifestValidationInput.model_validate(event)
    except Exception as exc:
        logger.error("Input validation failed", extra={"error": str(exc)})
        return ManifestValidationOutput(
            validation_status="FAILED",
            sample_count=0,
            errors=[{"sample_id": "", "rule": "schema_error", "detail": str(exc)}],
            manifest=None,
        ).model_dump()

    logger.info(
        "Validating manifest",
        extra={
            "cohort_id": input_model.cohort_id,
            "target_region": input_model.target_region,
        },
    )

    # Resolve manifest: fetch from S3 if it's a URI string
    try:
        if isinstance(input_model.sample_manifest, str):
            manifest_dict = _fetch_manifest_from_s3(input_model.sample_manifest)
        else:
            manifest_dict = input_model.sample_manifest
    except Exception as exc:
        logger.error("Failed to resolve manifest", extra={"error": str(exc)})
        return ManifestValidationOutput(
            validation_status="FAILED",
            sample_count=0,
            errors=[{"sample_id": "", "rule": "manifest_resolution", "detail": str(exc)}],
            manifest=None,
        ).model_dump()

    # Build SampleManifest model
    try:
        # Ensure cohort_id is present in the manifest dict
        if "cohort_id" not in manifest_dict:
            manifest_dict["cohort_id"] = input_model.cohort_id
        # Default reference_build if not present
        if "reference_build" not in manifest_dict:
            manifest_dict["reference_build"] = "GRCh38"
        manifest = SampleManifest.model_validate(manifest_dict)
    except Exception as exc:
        logger.error("Manifest schema validation failed", extra={"error": str(exc)})
        return ManifestValidationOutput(
            validation_status="FAILED",
            sample_count=0,
            errors=[{"sample_id": "", "rule": "manifest_schema", "detail": str(exc)}],
            manifest=None,
        ).model_dump()

    # Run validation logic (duplicate IDs, format checks, region checks)
    # Temporarily override TARGET_REGION for the validation call
    import gatk_sv_aws.orchestrator as _orch

    original_region = _orch.TARGET_REGION
    _orch.TARGET_REGION = input_model.target_region
    try:
        issues = _validate_manifest(
            manifest,
            region_resolver=_region_resolver,
        )
    finally:
        _orch.TARGET_REGION = original_region

    # Build response
    if issues:
        errors = [
            {
                "sample_id": issue.sample_id,
                "rule": issue.rule,
                "detail": issue.detail,
            }
            for issue in issues
        ]
        logger.warning(
            "Manifest validation failed",
            extra={
                "cohort_id": input_model.cohort_id,
                "error_count": len(errors),
            },
        )
        return ManifestValidationOutput(
            validation_status="FAILED",
            sample_count=len(manifest.samples),
            errors=errors,
            manifest=None,
        ).model_dump()

    logger.info(
        "Manifest validation passed",
        extra={
            "cohort_id": input_model.cohort_id,
            "sample_count": len(manifest.samples),
        },
    )
    return ManifestValidationOutput(
        validation_status="PASSED",
        sample_count=len(manifest.samples),
        errors=[],
        manifest=manifest_dict,
    ).model_dump()
