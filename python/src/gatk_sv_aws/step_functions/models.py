"""Data models for Lambda input/output contracts in the Step Functions orchestrator.

Defines Pydantic v2 models for the four Lambda handlers:
- validate-manifest (ManifestValidationInput/Output)
- start-run (StartRunInput/Output)
- poll-status (PollStatusInput/Output)
- gather-cost (GatherCostInput/Output, CostReportEntry, CostReport)

Also defines the state machine input/output contracts:
- StateMachineInput (Req 9.1, 9.4)
- StateMachineOverrides (optional overrides)
- PipelineSuccessOutput (Req 9.2)
- PipelineFailureOutput (Req 9.3)

All models use ``ConfigDict(extra="forbid")`` for strict validation,
matching the conventions in
:mod:`gatk_sv_aws.models`.

Requirements: 9.1, 9.2, 9.3, 9.4.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from gatk_sv_aws.models import ModuleName


# ---------------------------------------------------------------------------
# validate-manifest Lambda contracts
# ---------------------------------------------------------------------------


class ManifestValidationInput(BaseModel):
    """Input to the validate-manifest Lambda.

    Accepts either an inline manifest dict or an S3 URI string pointing
    to the manifest JSON.
    """

    model_config = ConfigDict(extra="forbid")

    cohort_id: str = Field(
        ...,
        min_length=1,
        description="Cohort identifier for this pipeline execution.",
    )
    sample_manifest: dict | str = Field(
        ...,
        description=(
            "Sample manifest — either an inline dict with a 'samples' key, "
            "or an S3 URI string (s3://bucket/key.json) to resolve."
        ),
    )
    output_uri: str = Field(
        ...,
        min_length=1,
        description="S3 output prefix for the cohort run.",
    )
    target_region: str = Field(
        default="ap-southeast-1",
        min_length=1,
        description="AWS region where all resources must reside.",
    )


class ManifestValidationOutput(BaseModel):
    """Output from the validate-manifest Lambda."""

    model_config = ConfigDict(extra="forbid")

    validation_status: Literal["PASSED", "FAILED"] = Field(
        ...,
        description="Whether the manifest passed all validation checks.",
    )
    sample_count: int = Field(
        ...,
        ge=0,
        description="Number of samples in the validated manifest.",
    )
    errors: list[dict] = Field(
        default_factory=list,
        description=(
            "List of validation errors. Each entry contains sample_id, "
            "rule, and detail keys."
        ),
    )
    manifest: dict | None = Field(
        default=None,
        description="Resolved manifest dict (None when validation fails).",
    )


# ---------------------------------------------------------------------------
# start-run Lambda contracts
# ---------------------------------------------------------------------------


class StartRunInput(BaseModel):
    """Input to the start-run Lambda."""

    model_config = ConfigDict(extra="forbid")

    module: ModuleName = Field(
        ...,
        description="GATK-SV module to execute.",
    )
    workflow_id: str = Field(
        ...,
        min_length=1,
        description="HealthOmics workflow ID for the module.",
    )
    workflow_version_name: str = Field(
        ...,
        min_length=1,
        description="HealthOmics workflow version name.",
    )
    parameters: dict = Field(
        default_factory=dict,
        description="Workflow parameters to pass to StartRun.",
    )
    output_uri: str = Field(
        ...,
        min_length=1,
        description="S3 output URI for this module's run.",
    )
    cohort_id: str = Field(
        ...,
        min_length=1,
        description="Cohort identifier for cost-tracking tags.",
    )
    sample_count: int = Field(
        ...,
        ge=1,
        description="Number of samples in the cohort.",
    )
    attempt_number: int = Field(
        default=1,
        ge=1,
        description="Current retry attempt number (1-based).",
    )


class StartRunOutput(BaseModel):
    """Output from the start-run Lambda."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(
        ...,
        min_length=1,
        description="HealthOmics run identifier.",
    )
    arn: str = Field(
        ...,
        min_length=1,
        description="Full ARN of the HealthOmics run.",
    )
    status: str = Field(
        ...,
        min_length=1,
        description="Initial run status (typically PENDING).",
    )
    module: ModuleName = Field(
        ...,
        description="Module that was submitted.",
    )
    attempt_number: int = Field(
        ...,
        ge=1,
        description="Attempt number for this submission.",
    )


# ---------------------------------------------------------------------------
# poll-status Lambda contracts
# ---------------------------------------------------------------------------


class PollStatusInput(BaseModel):
    """Input to the poll-status Lambda."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(
        ...,
        min_length=1,
        description="HealthOmics run identifier to poll.",
    )
    module: ModuleName = Field(
        ...,
        description="Module being polled (for logging context).",
    )
    cohort_id: str = Field(
        ...,
        min_length=1,
        description="Cohort identifier (for logging context).",
    )
    attempt_number: int = Field(
        default=1,
        ge=1,
        description="Current retry attempt number.",
    )
    module_start_time: str | None = Field(
        default=None,
        description=(
            "ISO 8601 timestamp of when the module was first submitted. "
            "Used for 24-hour timeout detection."
        ),
    )


class PollStatusOutput(BaseModel):
    """Output from the poll-status Lambda."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(
        ...,
        min_length=1,
        description="HealthOmics run identifier.",
    )
    status: str = Field(
        ...,
        min_length=1,
        description="Current run status (PENDING, STARTING, RUNNING, COMPLETED, FAILED, CANCELLED, MODULE_TIMEOUT).",
    )
    output_uri: str | None = Field(
        default=None,
        description="S3 output URI (populated on COMPLETED).",
    )
    is_terminal: bool = Field(
        ...,
        description="True if status is COMPLETED, FAILED, or CANCELLED.",
    )
    is_cache_hit: bool = Field(
        default=False,
        description="True if the run was satisfied from the run cache.",
    )
    failure_reason: str | None = Field(
        default=None,
        description="HealthOmics failure reason (populated on FAILED).",
    )
    error_code: str | None = Field(
        default=None,
        description="Error code for retry classification (populated on FAILED).",
    )
    duration_seconds: int | None = Field(
        default=None,
        description="Run duration in seconds (populated on terminal states).",
    )


# ---------------------------------------------------------------------------
# gather-cost Lambda contracts
# ---------------------------------------------------------------------------


class GatherCostInput(BaseModel):
    """Input to the gather-cost Lambda."""

    model_config = ConfigDict(extra="forbid")

    cohort_id: str = Field(
        ...,
        min_length=1,
        description="Cohort identifier.",
    )
    sample_count: int = Field(
        ...,
        ge=1,
        description="Number of samples in the cohort.",
    )
    output_uri: str = Field(
        ...,
        min_length=1,
        description="S3 output prefix where cost-report.json will be written.",
    )
    module_runs: list[dict] = Field(
        ...,
        description=(
            "List of module run records. Each dict contains module, run_id, "
            "status, and is_cache_hit keys."
        ),
    )


class CostReportEntry(BaseModel):
    """One module's cost entry in the cost report."""

    model_config = ConfigDict(extra="forbid")

    module: ModuleName = Field(
        ...,
        description="GATK-SV module name.",
    )
    run_id: str = Field(
        ...,
        min_length=1,
        description="HealthOmics run identifier.",
    )
    cost_usd: float = Field(
        ...,
        ge=0,
        description="Cost in USD for this module's run.",
    )
    duration_seconds: int = Field(
        ...,
        ge=0,
        description="Run duration in seconds.",
    )
    is_cache_hit: bool = Field(
        ...,
        description="Whether this run was a cache hit.",
    )


class CostReport(BaseModel):
    """Full cost report produced at pipeline completion."""

    model_config = ConfigDict(extra="forbid")

    cohort_id: str = Field(
        ...,
        min_length=1,
        description="Cohort identifier.",
    )
    sample_count: int = Field(
        ...,
        ge=1,
        description="Number of samples in the cohort.",
    )
    total_cost_usd: float = Field(
        ...,
        ge=0,
        description="Sum of all module costs.",
    )
    per_sample_cost_usd: float = Field(
        ...,
        ge=0,
        description="total_cost_usd / sample_count.",
    )
    modules: list[CostReportEntry] = Field(
        default_factory=list,
        description="Per-module cost breakdown.",
    )
    generated_at: str = Field(
        ...,
        min_length=1,
        description="ISO 8601 timestamp when the report was generated.",
    )


class GatherCostOutput(BaseModel):
    """Output from the gather-cost Lambda."""

    model_config = ConfigDict(extra="forbid")

    cost_report: CostReport = Field(
        ...,
        description="Full cost report.",
    )
    cost_report_uri: str = Field(
        ...,
        min_length=1,
        description="S3 URI where cost-report.json was written.",
    )


# ---------------------------------------------------------------------------
# State Machine input/output contracts (Req 9.1, 9.2, 9.3, 9.4)
# ---------------------------------------------------------------------------


class StateMachineOverrides(BaseModel):
    """Optional overrides for the state machine execution."""

    model_config = ConfigDict(extra="forbid")

    storage_type: Literal["DYNAMIC", "STATIC"] | None = Field(
        default=None,
        description="Storage type override (default DYNAMIC).",
    )
    cache_id: str | None = Field(
        default=None,
        description="Run cache ID override (default from stack parameter).",
    )
    networking_mode: Literal["RESTRICTED", "VPC"] | None = Field(
        default=None,
        description="Networking mode override (default RESTRICTED).",
    )


class StateMachineInput(BaseModel):
    """Input schema for the GATK-SV pipeline state machine.

    Validated as the first step before any HealthOmics runs are submitted.
    Required fields: cohort_id, sample_manifest, output_uri.
    """

    model_config = ConfigDict(extra="forbid")

    cohort_id: str = Field(
        ...,
        min_length=1,
        description="Cohort identifier for this pipeline execution.",
    )
    sample_manifest: dict | str = Field(
        ...,
        description=(
            "Sample manifest — either an inline dict with a 'samples' key, "
            "or an S3 URI string (s3://bucket/key.json) to resolve."
        ),
    )
    output_uri: str = Field(
        ...,
        min_length=1,
        description="S3 output prefix for the cohort run (e.g. s3://bucket/prefix).",
    )
    overrides: StateMachineOverrides | None = Field(
        default=None,
        description="Optional execution overrides for storage, cache, and networking.",
    )


class ModuleRunSummary(BaseModel):
    """Summary of a single module run in the pipeline output."""

    model_config = ConfigDict(extra="forbid")

    module: ModuleName = Field(
        ...,
        description="GATK-SV module name.",
    )
    run_id: str = Field(
        ...,
        min_length=1,
        description="HealthOmics run identifier.",
    )
    duration_seconds: int = Field(
        ...,
        ge=0,
        description="Run duration in seconds.",
    )
    is_cache_hit: bool = Field(
        ...,
        description="Whether this run was satisfied from the run cache.",
    )


class PipelineSuccessOutput(BaseModel):
    """Output produced when the pipeline completes successfully.

    Contains cohort_id, status=COMPLETED, module_runs (length 10),
    total_cost_usd, per_sample_cost_usd, output_uri, duration_seconds,
    and cost_report_uri.
    """

    model_config = ConfigDict(extra="forbid")

    cohort_id: str = Field(
        ...,
        min_length=1,
        description="Cohort identifier.",
    )
    status: Literal["COMPLETED"] = Field(
        default="COMPLETED",
        description="Terminal status indicating successful completion.",
    )
    module_runs: list[ModuleRunSummary] = Field(
        ...,
        description="Summary of each module run (should be length 10 for full pipeline).",
    )
    total_cost_usd: float = Field(
        ...,
        ge=0,
        description="Total pipeline cost in USD.",
    )
    per_sample_cost_usd: float = Field(
        ...,
        ge=0,
        description="Cost per sample in USD (total_cost_usd / sample_count).",
    )
    output_uri: str = Field(
        ...,
        min_length=1,
        description="S3 output prefix for the cohort run.",
    )
    duration_seconds: int = Field(
        ...,
        ge=0,
        description="Total pipeline duration in seconds.",
    )
    cost_report_uri: str = Field(
        ...,
        min_length=1,
        description="S3 URI of the cost-report.json file.",
    )


class CompletedModuleSummary(BaseModel):
    """Summary of a completed module in a failed pipeline output."""

    model_config = ConfigDict(extra="forbid")

    module: ModuleName = Field(
        ...,
        description="GATK-SV module name.",
    )
    run_id: str = Field(
        ...,
        min_length=1,
        description="HealthOmics run identifier.",
    )


class PipelineFailureOutput(BaseModel):
    """Output produced when the pipeline fails.

    Contains cohort_id, status=FAILED, failed_module, failed_run_id,
    error_message, error_code, retry_attempts, completed_modules,
    and partial_cost_report.
    """

    model_config = ConfigDict(extra="forbid")

    cohort_id: str = Field(
        ...,
        min_length=1,
        description="Cohort identifier.",
    )
    status: Literal["FAILED"] = Field(
        default="FAILED",
        description="Terminal status indicating pipeline failure.",
    )
    failed_module: ModuleName = Field(
        ...,
        description="Module that caused the pipeline failure.",
    )
    failed_run_id: str = Field(
        ...,
        min_length=1,
        description="HealthOmics run ID of the failed run.",
    )
    error_message: str = Field(
        ...,
        min_length=1,
        description="Human-readable error message from the failed run.",
    )
    error_code: str | None = Field(
        default=None,
        description="Error code for classification (e.g. InternalServerError).",
    )
    retry_attempts: int = Field(
        ...,
        ge=0,
        description="Number of retry attempts made before failure.",
    )
    completed_modules: list[CompletedModuleSummary] = Field(
        default_factory=list,
        description="Modules that completed successfully before the failure.",
    )
    partial_cost_report: dict | None = Field(
        default=None,
        description="Partial cost report for completed modules (None if unavailable).",
    )
